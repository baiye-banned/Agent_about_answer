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
  只有标记而无理由时不豁免；而形态命中在**标记确实被咨询过**的前提下依然豁免不了——
  这一条由双路径夹具（大写键名 + `sk-` 形态同行）钉住，否则「标记对形态无效」与
  「标记压根没被咨询」这两种失效不可区分，断言会退化成恒真的空话；
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
- 判红样本里每个夹具文件只命中一条规则（赋值启发式区分大小写，故夹具的变量名用小写），这样
  变异测试里「某条规则失效」与「某个夹具不再报红」是一一对应的，不会互相掩护；豁免边界那组
  有一个刻意的例外——双路径夹具同行命中两条规则，且不登记进 `SHAPES`，因此不参与上面那张
  映射表，它存在的意义是让标记先被咨询、再证明它的效力到此为止；
- 八次子进程调用（五条规则的样本合并到同一目录、豁免边界四侧合并到同一次扫描，减少进程启动
  开销），无网络、无凭据依赖；
- 需要的是脚本头部声明的那个运行环境：git-bash / POSIX 的 bash。Windows 上
  `shutil.which("bash")` 若优先命中 WSL 的 `System32\bash.exe`，那种 bash 读不了 `C:/…`
  形式的路径，用例会整批以退出码 127 失败——那是环境不对，不是门禁退化；从 git-bash 里跑，
  或让 Git 的 `usr\bin` 排在 PATH 前面即可。
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
# 这不违反 DEVELOPING.md 里「禁止 --patterns-only 之类跳过扫描的做法进入 CI」：那条规矩拦的是
# **拿它扫仓库**、让门禁静默少扫一层；仓库自身的扫描完全不受影响，secret-scan.yml 仍以默认
# 模式全量跑（gitleaks 缺失即退出 2）。这里 --patterns-only 只作用于 tmp_path 里的夹具目录，
# 而且用例会断言脚本确实打印了跳过说明——跳过永远是显式的，不会变成一次「干净」的假绿。
PATTERNS_ONLY = "--patterns-only"
GITLEAKS_SKIP_NOTE = "--patterns-only - gitleaks skipped"

# 变异测试用的「永不匹配」正则体：普通字面量，不会出现在任何夹具里。
NEVER_MATCHES = "scan_selftest_mutation_never_matches"

# 代码里的标记文本按片段拼出来：本文件既要演示标记的判定，又紧挨着各种赋值形态的示例，
# 若把它写成完整字面量，将来只要有人在同一行补一个「大写变量名 + 取值」的样例，仓库扫描就会
# 多出一条豁免记录，看起来像这份测试在藏东西。完整的标记写法只在模块 docstring 的说明里出现
# 一次（那一行不含赋值形态，因此不会被判成豁免）。改本文件时请照此办理。
ALLOW_MARK = "# scan-secrets:" + "allow"

ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"
UPPER_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# 随机体开头固定成一段不会命中占位词表的字符：`is_not_secret` 会把 xxx* / fake* / your[-_]*
# / testkey* 这类前缀判成配置而不是凭据，纯随机串偶发撞上（`xxx` 开头约 1/46656）就会让夹具
# 在某个无关提交上随机转红。开头固定、其余仍随机——命中形态不变，抖动消失。
SAFE_HEAD = "q7z"
SAFE_HEAD_UPPER = "Q7Z"


def _body(alphabet, length, head):
    """返回以 head 开头、总长 length 的随机串，字符取自 alphabet。"""
    return head + "".join(secrets.choice(alphabet) for _ in range(length - len(head)))


# ---------------------------------------------------------------------------
# 五条内置规则的判红样本
#
# 每个夹具是一行独立的文件内容，且只命中它对应的那一条规则。前缀与随机体分开写，
# 是为了让这个文件（它自己也在扫描范围内）不出现可命中的完整形态。
# ---------------------------------------------------------------------------


def _sk_line():
    return 'client_key = "%s"' % ("sk-" + _body(ALNUM, 24, SAFE_HEAD))


def _aws_line():
    return 'aws_access_id = "%s"' % ("AKIA" + _body(UPPER_ALNUM, 16, SAFE_HEAD_UPPER))


def _github_line():
    return 'vcs_credential = "%s"' % ("ghp_" + _body(ALNUM, 24, SAFE_HEAD))


def _pem_line():
    return "-----BEGIN " + "RSA " + "PRIVATE KEY-----"


def _assignment_line():
    # 赋值的变量名必须大写才会命中启发式（脚本刻意只匹配大写前缀，见文件头注释），
    # 取值是随机串而不是占位词，否则会被 `is_not_secret` 判成配置而非凭据。
    return "%s=%s" % ("PASSWORD", _body(ALNUM, 24, SAFE_HEAD))


def _sk_assignment_line():
    """一行同时命中赋值启发式与 `sk-` 形态：键名大写，所以这行进得了豁免判定分支。

    这一点是它存在的全部理由。脚本把豁免标记的判定嵌在 `[[ $content =~ $RE_PREFIX ]]`
    分支**内部**，而 `RE_PREFIX` 只认大写键名并区分大小写——所以拿小写键名（如 `_sk_line`
    的 `client_key`）去断言「标记没能豁免形态命中」，得到的是一条恒真断言：那行压根没走到
    豁免判定。要证明「标记对形态命中无效」，先得让标记有机会对它生效。
    """
    return '%s = "%s"' % ("API_KEY", "sk-" + _body(ALNUM, 24, SAFE_HEAD))


# (匹配类型标签, 夹具文件名, 生成判红内容的函数)
SHAPES = [
    ("sk- token", "planted_sk.py", _sk_line),
    ("aws access key id", "planted_aws.py", _aws_line),
    ("github token", "planted_github.py", _github_line),
    ("private key header", "planted_pem.py", _pem_line),
    ("credential assignment", "planted_assignment.py", _assignment_line),
]

# 替换掉判红样本的普通文本：同一批文件、同样的文件名，只是没有密钥。
# 刻意不含「大写变量名 + 取值」这类形态：那类串是否算凭据只有 gitleaks（Layer 2）的熵规则
# 判得了，而本文件只跑 Layer 1，留一行「结果要到 CI 才知道」的文本不值当——判绿样本本来
# 也不需要长成赋值的样子。
BENIGN = "#!/usr/bin/env python3\n# ordinary module source\nvalue = 1\n"


def _run_scan(target, script=SCAN_SCRIPT):
    """在 `target` 目录上跑一次内置扫描，返回 (退出码, stdout, stderr)。"""
    result = subprocess.run(
        [BASH, str(script.as_posix()), str(Path(target).as_posix()), PATTERNS_ONLY],
        capture_output=True,
        text=True,
        # 固定 UTF-8 并允许替换：断言只看 ASCII 片段（文件名、匹配类型、文件数），
        # 但路径里可能出现非 ASCII（例如中文用户名），不能让解码在此抛 UnicodeDecodeError。
        encoding="utf-8",
        errors="replace",
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
    """豁免标记的四条边界。

    四个文件放在同一次扫描里，一次运行同时给出四侧结论——豁免既没有被静默，也没有被放大到
    形态命中上：

    - 带理由的赋值行：进豁免清单（stdout），不再报红；
    - 只有标记、没有理由的赋值行：不豁免，仍按命中报红（stderr）；
    - 形态命中 + 完整标记：标记对它无效，照常报红（stderr）且不进豁免清单；
    - 双路径（大写键名 + `sk-` 形态同行）：赋值命中被豁免（stdout）、形态命中照常报红
      （stderr）。前三侧各自只钉一头，这一侧把两头钉在同一行上，才排得掉「标记压根没被
      咨询」——详见下面断言的注释。
    """
    allowed = "planted_allowed.py"
    bare = "planted_bare_allow.py"
    shape = "planted_shape_allow.py"
    dual = "planted_shape_and_assignment_allow.py"
    reason = "%s real fixture value, not a credential\n" % ALLOW_MARK
    _write_all(
        tmp_path,
        {
            allowed: _assignment_line() + "  " + reason,
            bare: _assignment_line() + "  " + ALLOW_MARK + "\n",
            # 形态命中这一侧是这套规则里最要紧的承诺：脚本保证标记只豁免赋值启发式，`sk-`
            # 这类形态命中永远豁免不了（见脚本头部与 DEVELOPING.md）。一旦失效，一个标记就能
            # 把真正的密钥藏起来——门禁会从「兜底」变成「帮凶」，所以必须有断言盯着它。
            # 但只凭这一侧盯不住：小写键名进不了 RE_PREFIX 分支，标记从未被咨询，断言恒真。
            shape: _sk_line() + "  " + reason,
            # 所以再补一条双路径夹具，把承诺钉实（理由见该断言的注释）。
            dual: _sk_assignment_line() + "  " + reason,
        },
    )

    rc, stdout, stderr = _run_scan(tmp_path)

    assert rc == 1, (stdout, stderr)
    # 带理由的赋值行：进豁免清单，不再出现在命中列表里。
    assert "%s:1 [credential assignment]" % allowed in stdout, stdout
    assert "%s:1 [credential assignment]" % allowed not in stderr, stderr
    # 只有标记、没有理由的赋值行：不豁免，仍然报红。
    assert "%s:1 [credential assignment]" % bare in stderr, stderr
    # 形态命中 + 完整标记：标记对它无效，必须照常报红，也不得出现在豁免清单里。
    assert "%s:1 [sk- token]" % shape in stderr, stderr
    assert "%s:1" % shape not in stdout, stdout
    # 双路径夹具：「标记藏不住真密钥」这条属性的正面证据。同一行里赋值命中与形态命中并存，
    # 于是四条断言把两种失效方式分得开——
    #   若豁免逻辑退化成「有标记就整行放过」：前两条仍绿，形态命中那两条转红；
    #   若标记压根不再被咨询（例如 RE_ALLOW 取不到、判定被移出分支）：第 1、2 条转红，
    #   因为赋值命中会掉回命中列表，而不是进豁免清单。
    # 换句话说，前两条断言证明标记对这一行**生效过**，后两条证明它的效力**到此为止**。
    # 少了前两条，「形态命中永不豁免」就还是那句恒真的空话。
    assert "%s:1 [credential assignment]" % dual in stdout, stdout
    assert "%s:1 [credential assignment]" % dual not in stderr, stderr
    assert "%s:1 [sk- token]" % dual in stderr, stderr
    assert "%s:1 [sk- token]" % dual not in stdout, stdout


# ---------------------------------------------------------------------------
# 变异测试：删掉任意一条规则，必须有用例转红
#
# 每个参数把脚本副本里的一条正则换成永不匹配的形态，再拿该规则的判红样本去扫。
# 变异后必须恰好退出 0（真的扫到了那个夹具、且什么都没命中）；若脚本因变异而坏掉会得到 2，
# 用例同样失败。
#
# 与上面判红用例的关系（别把它读成同一件事的两种写法）：变异用例断言「正则没了 → 样本不再
# 报红」，判红用例断言「正则还在 → 样本报红」，两条是互补的两半，合起来才构成 issue 要求的
# 先红后绿证据。反过来说，单独删掉判红用例，这五条变异用例会退化成弱断言（它们并不会因此
# 失败）——这层耦合是刻意的：两半都在同一文件的同一次 CI 运行里执行，评审本文件时应把两者
# 当一个整体看。
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

    rc, stdout, stderr = _run_scan(fixtures, script=mutated_script)

    # rc 必须恰好是 0：脚本坏掉（用法错误、grep 失败）会得到 2，同样判失败，不会被放过。
    assert rc == 0, "删掉 %s 后 %s 仍被判红（rc=%s）：说明该样本并非由这条正则命中" % (
        variable,
        name,
        rc,
    )
    # 还必须真的扫到了那个夹具。非 git 目录下脚本没有「0 个文件」的兜底（脚本里那道兜底
    # 只在 GIT_ROOT 非空时生效），collect_files 一旦退化，这里会拿「扫了 0 个文件」的干净
    # 结果退出 0，五个变异用例就一起空转通过了。所以文件数和判绿用例一样要断言。
    assert "built-in pattern scan over 1 file(s)" in stdout, stdout
    assert "[%s]" % label not in stderr, stderr
