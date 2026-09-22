"""MYSQL_PASSWORD 缺失或仍是仓库公开占位值时的启动门禁（issue #185）。

核心不变量：仓库内置的 `change-me` 永远不能成为进程实际拿去连库的口令——未显式配置
MYSQL_PASSWORD 时服务必须在启动路径上拒绝启动。这道闸门与 SECRET_KEY 那道具同源同强度，
但必须**各自独立**：任何一个放行开关都不能顺带放松另一项。
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
STRONG_MYSQL_PASSWORD = "test-only-strong-mysql-password-9f3a1c7e5b2d8046"
STRONG_SECRET_KEY = "test-only-strong-secret-key-4f8c2a1d9e7b3c5a6d0f2e8b1a7c4d9e"

_GUARD_ENV_VARS = (
    "MYSQL_PASSWORD",
    "ALLOW_INSECURE_DEFAULT_MYSQL_PASSWORD",
    "SECRET_KEY",
    "ALLOW_INSECURE_DEFAULT_SECRET",
)


def _run_in_subprocess(script: str, **env_overrides):
    env = dict(os.environ)
    for name in _GUARD_ENV_VARS:
        env.pop(name, None)
    # 显式置空：空值等价于「未配置」，同时因为 load_dotenv(override=False) 不会覆盖已存在的键，
    # 可以挡住开发机本地 .env 里的同名设置，保证用例可复现。
    for name in _GUARD_ENV_VARS:
        env[name] = ""
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _warning_records(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# password resolution
# ---------------------------------------------------------------------------


def test_resolve_mysql_password_uses_explicit_env_value():
    password, source = config.resolve_mysql_password(STRONG_MYSQL_PASSWORD, False)

    assert (password, source) == (STRONG_MYSQL_PASSWORD, config.MYSQL_PASSWORD_SOURCE_ENV)


def test_resolve_mysql_password_never_returns_the_repo_placeholder():
    password, source = config.resolve_mysql_password(config.DEFAULT_MYSQL_PASSWORD, False)

    assert password != config.DEFAULT_MYSQL_PASSWORD
    assert source == config.MYSQL_PASSWORD_SOURCE_UNCONFIGURED
    assert len(password) >= 32


@pytest.mark.parametrize("value", [None, "", "   ", " change-me ", "\tchange-me\n"])
def test_resolve_mysql_password_treats_blank_and_padded_placeholder_as_missing(value):
    password, source = config.resolve_mysql_password(value, False)

    assert password != config.DEFAULT_MYSQL_PASSWORD
    assert source == config.MYSQL_PASSWORD_SOURCE_UNCONFIGURED


def test_resolve_mysql_password_strips_a_configured_value():
    password, source = config.resolve_mysql_password(f"  {STRONG_MYSQL_PASSWORD}  ", False)

    assert password == STRONG_MYSQL_PASSWORD
    assert source == config.MYSQL_PASSWORD_SOURCE_ENV


def test_resolve_mysql_password_insecure_switch_allows_the_placeholder():
    password, source = config.resolve_mysql_password(config.DEFAULT_MYSQL_PASSWORD, True)

    assert (password, source) == (
        config.DEFAULT_MYSQL_PASSWORD,
        config.MYSQL_PASSWORD_SOURCE_INSECURE_DEV,
    )


def test_database_url_never_carries_the_public_placeholder_without_the_switch():
    """不变量落地到 DATABASE_URL：闸门之外的路径也拿不到那个可猜口令。"""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "from config import DATABASE_URL, DEFAULT_MYSQL_PASSWORD, MYSQL_PASSWORD\n"
        "assert MYSQL_PASSWORD != DEFAULT_MYSQL_PASSWORD, MYSQL_PASSWORD\n"
        "assert DEFAULT_MYSQL_PASSWORD not in DATABASE_URL, DATABASE_URL\n"
        "print('DATABASE_URL_HAS_NO_DEFAULT')\n"
    )

    result = _run_in_subprocess(script)

    assert result.returncode == 0, result.stderr
    assert "DATABASE_URL_HAS_NO_DEFAULT" in result.stdout


# ---------------------------------------------------------------------------
# startup gate
# ---------------------------------------------------------------------------


def test_startup_check_raises_with_actionable_message_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "MYSQL_PASSWORD_SOURCE", config.MYSQL_PASSWORD_SOURCE_UNCONFIGURED)

    with pytest.raises(config.MysqlPasswordError) as exc_info:
        config.ensure_mysql_password_configured()

    message = str(exc_info.value)
    assert "MYSQL_PASSWORD" in message
    assert "ALLOW_INSECURE_DEFAULT_MYSQL_PASSWORD" in message
    # 可操作提示：给出生成随机口令的命令，而不只是说「配置一下」。
    assert "secrets.token_urlsafe" in message
    assert config.DEFAULT_MYSQL_PASSWORD not in message  # 不把占位口令当成可用口令回显


def test_startup_check_raises_when_the_placeholder_password_is_configured(monkeypatch):
    password, source = config.resolve_mysql_password(config.DEFAULT_MYSQL_PASSWORD, False)
    monkeypatch.setattr(config, "MYSQL_PASSWORD", password)
    monkeypatch.setattr(config, "MYSQL_PASSWORD_SOURCE", source)

    with pytest.raises(config.MysqlPasswordError):
        config.ensure_mysql_password_configured()


def test_startup_check_passes_for_explicit_password(monkeypatch):
    """阳性对照：守卫不能实现成「永远拒绝」。"""
    password, source = config.resolve_mysql_password(STRONG_MYSQL_PASSWORD, False)
    monkeypatch.setattr(config, "MYSQL_PASSWORD", password)
    monkeypatch.setattr(config, "MYSQL_PASSWORD_SOURCE", source)

    assert config.ensure_mysql_password_configured() is None


def test_startup_check_passes_with_insecure_dev_switch(monkeypatch):
    password, source = config.resolve_mysql_password(config.DEFAULT_MYSQL_PASSWORD, True)
    monkeypatch.setattr(config, "MYSQL_PASSWORD", password)
    monkeypatch.setattr(config, "MYSQL_PASSWORD_SOURCE", source)

    assert config.ensure_mysql_password_configured() is None


def test_insecure_switch_logs_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        config.resolve_mysql_password(None, True)

    warnings = _warning_records(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage()
    assert "ALLOW_INSECURE_DEFAULT_MYSQL_PASSWORD" in message
    # 放行不是静默放行：必须点名「这会用公开占位口令连库」和「禁止对外部署」。
    assert "placeholder" in message
    assert "reachable by others" in message


def test_unconfigured_refusal_logs_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        config.resolve_mysql_password(None, False)

    warnings = _warning_records(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "MYSQL_PASSWORD" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# the two switches stay independent
# ---------------------------------------------------------------------------


def test_mysql_switch_does_not_relax_the_secret_key_gate():
    """MYSQL 放行开关打开，SECRET_KEY 未配置时仍必须拒绝启动。"""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "import config\n"
        "config.ensure_mysql_password_configured()\n"
        "try:\n"
        "    config.ensure_secret_key_configured()\n"
        "except config.SecretKeyError:\n"
        "    print('SECRET_KEY_STILL_REFUSED')\n"
        "else:\n"
        "    raise AssertionError('MYSQL 开关放行了 SECRET_KEY 门禁')\n"
    )

    result = _run_in_subprocess(
        script, ALLOW_INSECURE_DEFAULT_MYSQL_PASSWORD="true", SECRET_KEY=""
    )

    assert result.returncode == 0, result.stderr
    assert "SECRET_KEY_STILL_REFUSED" in result.stdout


def test_secret_key_switch_does_not_relax_the_mysql_gate():
    """SECRET_KEY 放行开关打开，MYSQL_PASSWORD 未配置时仍必须拒绝启动。"""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "import config\n"
        "config.ensure_secret_key_configured()\n"
        "try:\n"
        "    config.ensure_mysql_password_configured()\n"
        "except config.MysqlPasswordError:\n"
        "    print('MYSQL_STILL_REFUSED')\n"
        "else:\n"
        "    raise AssertionError('SECRET_KEY 开关放行了 MYSQL 门禁')\n"
    )

    result = _run_in_subprocess(
        script, ALLOW_INSECURE_DEFAULT_SECRET="true", MYSQL_PASSWORD=""
    )

    assert result.returncode == 0, result.stderr
    assert "MYSQL_STILL_REFUSED" in result.stdout


# ---------------------------------------------------------------------------
# end-to-end startup behaviour
# ---------------------------------------------------------------------------


def test_service_startup_exits_without_mysql_password():
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "from fastapi.testclient import TestClient\n"
        "import main\n"
        "with TestClient(main.app):\n"
        "    print('STARTUP_SUCCEEDED')\n"
    )

    result = _run_in_subprocess(script, SECRET_KEY=STRONG_SECRET_KEY)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "STARTUP_SUCCEEDED" not in result.stdout
    assert "MYSQL_PASSWORD" in output
    # 必须死于门禁本身：少了这一条，把 lifespan 里的 ensure_mysql_password_configured()
    # 删掉后用例会以「连不上数据库」为由同样退出，门禁被移除的回归就没人拦得住。
    assert "MysqlPasswordError" in output


def test_service_startup_check_passes_with_explicit_mysql_password():
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "from config import ensure_mysql_password_configured\n"
        "ensure_mysql_password_configured()\n"
        "print('STARTUP_CHECK_PASSED')\n"
    )

    result = _run_in_subprocess(script, SECRET_KEY=STRONG_SECRET_KEY, MYSQL_PASSWORD=STRONG_MYSQL_PASSWORD)

    assert result.returncode == 0, result.stderr
    assert "STARTUP_CHECK_PASSED" in result.stdout


def test_service_startup_allowed_by_insecure_switch_with_a_warning():
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "from config import (\n"
        "    DEFAULT_MYSQL_PASSWORD,\n"
        "    MYSQL_PASSWORD,\n"
        "    MYSQL_PASSWORD_SOURCE,\n"
        "    MYSQL_PASSWORD_SOURCE_INSECURE_DEV,\n"
        "    ensure_mysql_password_configured,\n"
        ")\n"
        "ensure_mysql_password_configured()\n"
        "assert MYSQL_PASSWORD == DEFAULT_MYSQL_PASSWORD, MYSQL_PASSWORD\n"
        "assert MYSQL_PASSWORD_SOURCE == MYSQL_PASSWORD_SOURCE_INSECURE_DEV, MYSQL_PASSWORD_SOURCE\n"
        "print('INSECURE_DEV_STARTUP_OK')\n"
    )

    result = _run_in_subprocess(script, SECRET_KEY=STRONG_SECRET_KEY, ALLOW_INSECURE_DEFAULT_MYSQL_PASSWORD="true")

    output = result.stdout + result.stderr
    assert result.returncode == 0, result.stderr
    assert "INSECURE_DEV_STARTUP_OK" in result.stdout
    # 放行时必须留下痕迹，而不是静默启动：日志真的打到了进程输出。
    # （「这条日志的级别确实是 WARNING」由 test_insecure_switch_logs_a_warning 用 caplog 钉住。）
    assert "ALLOW_INSECURE_DEFAULT_MYSQL_PASSWORD" in output
    assert "placeholder" in output
