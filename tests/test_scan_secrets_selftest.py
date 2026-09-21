"""`scripts/scan_secrets.sh` 内置正则层（Layer 1）的回归自检（issue #54）。

门禁本身是密钥泄漏的最后一道兜底，但此前对它的自动化覆盖只有一条：`.github/workflows/secret-scan.yml`
里 `workflow_dispatch` + `self_test=true` 才跑的手动步骤，且只植入 `sk-` 一种形态。
`AKIA` / `ghp_` / PEM / 赋值启发式四条规则一旦被写坏，门禁只会安静地不再报红——而这个
「安静」不会被任何信号发现。本文件把那套自检变成随 `python -m pytest -q tests` 一起跑的用例。

覆盖内容：

- 五条内置规则（`sk-` / `AKIA` / `ghp_` / PEM / 赋值启发式）各有一个判红样本：退出码为 1，
  且输出按 `文件:行号 [匹配类型]` 点名对应文件；
- 同样的五个文件替换为普通文本后，退出码回到 0（判绿样本）；
- 豁免标记的边界：带非空理由的 `# scan-secrets:allow <理由>` 豁免该行赋值命中，
  只有标记而无理由时不豁免；
- 变异测试：把任一条正则改成永不匹配的形态后，针对该规则的判红样本必须不再报红。
  换句话说，「删掉某条规则」这件事会让上面至少一条断言转红——这正是 issue #54 要求的
  「先红后绿」证据，且每次 CI 都会重新验证一遍，而不是只留在 PR 描述里。

几处刻意的设计：

- 夹具全部在运行时生成、写进 `tmp_path`，仓库里不留任何真实形态的密钥串。本文件本身也在
  仓库扫描范围内，所以连「拼出密钥」的代码也写成前缀与随机体分离的形式（如
  `"AKIA" + 随机 16 位大写`），任何一行都不构成可命中的完整形态；
- 只调用 `--patterns-only`：本文件验证的是 Layer 1，gitleaks（Layer 2）缺失时必须是被明确
  标注的跳过而不是失败。脚本会打印 `--patterns-only - gitleaks skipped ...`，用例断言这行
  说明存在，避免「静默跳过」被误读成「扫过了」；
- 每个夹具文件只命中一条规则（赋值启发式区分大小写，故夹具的变量名用小写），这样变异测试
  里「某条规则失效」与「某个夹具不再报红」是一一对应的，不会互相掩护；
- 八次子进程调用（五个形态合并到同一目录，减少进程启动开销），无网络、无凭据依赖。
"""

import re
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCAN_SCRIPT = ROOT / "scripts" / "scan_secrets.sh"

# 脚本头部即声明面向 git-bash / POSIX shell；拿不到 bash 时整体跳过并说明原因，
# 而不是把「跑不了」伪装成「通过了」。
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(
    BASH is None, reason="需要 POSIX shell（bash）来执行 scripts/scan_secrets.sh"
)

# 扫的是临时目录，Layer 2 不参与：本文件只做内置正则的回归，gitleaks 缺失按明确跳过处理。
PATTERNS_ONLY = "--patterns-only"
GITLEAKS_SKIP_NOTE = "--patterns-only - gitleaks skipped"

# 变异测试用的「永不匹配」正则体：普通字面量，不会出现在任何夹具里。
NEVER_MATCHES = "scan_selftest_mutation_never_matches"

# 运行时拼出豁免标记，让本文件自身不含完整的标记文本——否则「标记 + 非空理由」若与某行
# 赋值形态同行出现，仓库扫描会在日志里多打印一条豁免，看起来像这份测试在藏东西。
ALLOW_MARK = "# scan-secrets:" + "allow"

ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"
UPPER_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _body(alphabet, length):
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# 五条内置规则的判红样本
#
# 每个夹具是一行独立的文件内容，且只命中它对应的那一条规则。前缀与随机体分开写，
# 是为了让这个文件（它自己也在扫描范围内）不出现可命中的完整形态。
# ---------------------------------------------------------------------------


def _sk_line():
    return 'client_key = "%s"' % ("sk-" + _body(ALNUM, 24))


def _aws_line():
    return 'aws_access_id = "%s"' % ("AKIA" + _body(UPPER_ALNUM, 16))


def _github_line():
    return 'vcs_credential = "%s"' % ("ghp_" + _body(ALNUM, 24))


def _pem_line():
    return "-----BEGIN " + "RSA " + "PRIVATE KEY-----"


def _assignment_line():
    # 赋值的变量名必须大写才会命中启发式（脚本刻意只匹配大写前缀，见文件头注释），
    # 取值是随机串而不是占位词，否则会被 `is_not_secret` 判成配置而非凭据。
    return "%s=%s" % ("PASSWORD", _body(ALNUM, 24))


# (匹配类型标签, 夹具文件名, 生成判红内容的函数)
SHAPES = [
    ("sk- token", "planted_sk.py", _sk_line),
    ("aws access key id", "planted_aws.py", _aws_line),
    ("github token", "planted_github.py", _github_line),
    ("private key header", "planted_pem.py", _pem_line),
    ("credential assignment", "planted_assignment.py", _assignment_line),
]

# 替换掉判红样本的普通文本：同一批文件、同样的文件名，只是没有密钥。
BENIGN = "#!/usr/bin/env python3\nvalue = 1\npassword = get_password()\n"


def _run_scan(target, script=SCAN_SCRIPT):
    """在 `target` 目录上跑一次内置扫描，返回 (退出码, stdout, stderr)。"""
    result = subprocess.run(
        [BASH, str(script.as_posix()), str(Path(target).as_posix()), PATTERNS_ONLY],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return result.returncode, result.stdout, result.stderr


def _write_all(directory, contents_by_name):
    for name, content in contents_by_name.items():
        (directory / name).write_text(content, encoding="utf-8")


def _assert_layer2_skip_is_announced(stdout, stderr):
    """gitleaks 的跳过必须是显式的：没有任何说明的「干净」才是危险的假绿。"""
    assert GITLEAKS_SKIP_NOTE in (stdout + stderr)


def test_every_builtin_shape_is_reported_and_names_the_file(tmp_path):
    fixtures = {name: build() for _, name, build in SHAPES}
    _write_all(tmp_path, fixtures)

    rc, stdout, stderr = _run_scan(tmp_path)

    assert rc == 1, stderr
    _assert_layer2_skip_is_announced(stdout, stderr)
    # 逐条规则点名：命中类型与文件必须一一对应（不打印匹配内容，这是脚本的既定约定）。
    for label, name, _ in SHAPES:
        assert "%s:1 [%s]" % (name, label) in stderr, (label, stderr)


def test_every_builtin_shape_is_green_once_the_fixtures_are_benign(tmp_path):
    _write_all(tmp_path, {name: BENIGN for _, name, _ in SHAPES})

    rc, stdout, stderr = _run_scan(tmp_path)

    assert rc == 0, (stdout, stderr)
    assert "built-in pattern scan clean" in stdout
    # 判绿不能是「什么都没扫」：脚本会报出文件数，夹具必须真的在扫描范围内。
    assert "built-in pattern scan over %d file(s)" % len(SHAPES) in stdout


def test_allow_marker_exempts_only_when_a_reason_follows(tmp_path):
    """豁免标记的边界：带非空理由豁免该行，只有标记而无理由不豁免。

    两个文件放在同一次扫描里：带理由的进豁免清单（stdout），只有标记的仍按命中报红
    （stderr）。同一次运行同时给出两侧结论——豁免既没有被静默，也没有被放大。
    """
    allowed, bare = "planted_allowed.py", "planted_bare_allow.py"
    _write_all(
        tmp_path,
        {
            allowed: "%s  %s real fixture value, not a credential\n" % (_assignment_line(), ALLOW_MARK),
            bare: "%s  %s\n" % (_assignment_line(), ALLOW_MARK),
        },
    )

    rc, stdout, stderr = _run_scan(tmp_path)

    assert rc == 1, (stdout, stderr)
    # 带理由：进豁免清单，不再出现在命中列表里。
    assert "%s:1 [credential assignment]" % allowed in stdout, stdout
    assert "%s:1 [credential assignment]" % allowed not in stderr, stderr
    # 只有标记、没有理由：不豁免，仍然报红。
    assert "%s:1 [credential assignment]" % bare in stderr, stderr


# ---------------------------------------------------------------------------
# 变异测试：删掉任意一条规则，必须有用例转红
#
# 每个参数把脚本副本里的一条正则换成永不匹配的形态，再拿该规则的判红样本去扫。
# 变异后必须恰好退出 0（真的扫描过、且什么都没命中）；若脚本因变异而坏掉会得到 2，
# 用例同样失败。这一断言等价于「删掉该条正则后，上面 test_every_builtin_shape_...
# 里对应那条 `文件:1 [匹配类型]` 断言会转红」——把 issue 要求的先红后绿证据固化成了
# 每次 CI 都会重跑的自检，而不是只写在 PR 描述里。
# ---------------------------------------------------------------------------

MUTATIONS = [
    ("RE_SK", "sk- token", "planted_sk.py", _sk_line),
    ("RE_AWS", "aws access key id", "planted_aws.py", _aws_line),
    ("RE_GH", "github token", "planted_github.py", _github_line),
    ("RE_PEM", "private key header", "planted_pem.py", _pem_line),
    ("RE_PREFIX", "credential assignment", "planted_assignment.py", _assignment_line),
]


def _mutate(script_text, variable):
    """把 `变量名='...'` 整行替换成永不匹配的正则，返回 (新文本, 替换次数)。"""
    pattern = re.compile(r"^%s='[^']*'$" % variable, re.MULTILINE)
    return pattern.subn("%s='%s'" % (variable, NEVER_MATCHES), script_text)


@pytest.mark.parametrize("variable, label, name, build", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_removing_a_rule_regex_stops_its_fixture_from_being_reported(
    tmp_path, variable, label, name, build
):
    mutated_text, replacements = _mutate(SCAN_SCRIPT.read_text(encoding="utf-8"), variable)
    assert replacements == 1, "预期 %s 在脚本里恰好出现一次赋值，实际 %d 次" % (variable, replacements)

    mutated_script = tmp_path / "scan_secrets_mutated.sh"
    mutated_script.write_text(mutated_text, encoding="utf-8")

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _write_all(fixtures, {name: build()})

    rc, _stdout, stderr = _run_scan(fixtures, script=mutated_script)

    # rc 必须恰好是 0：脚本坏掉（用法错误、grep 失败）会得到 2，同样判失败，不会被放过。
    assert rc == 0, "删掉 %s 后 %s 仍被判红（rc=%s）：说明该样本并非由这条正则命中" % (
        variable,
        name,
        rc,
    )
    assert "[%s]" % label not in stderr, stderr
