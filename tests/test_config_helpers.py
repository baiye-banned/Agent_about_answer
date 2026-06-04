from config import _env_bool, _env_int


def test_env_bool_uses_default_for_missing_or_blank(monkeypatch):
    monkeypatch.delenv("TEST_BOOL", raising=False)
    assert _env_bool("TEST_BOOL", True) is True

    monkeypatch.setenv("TEST_BOOL", "")
    assert _env_bool("TEST_BOOL", False) is False


def test_env_bool_parses_common_true_values(monkeypatch):
    for value in ["1", "true", "yes", "on", " TRUE "]:
        monkeypatch.setenv("TEST_BOOL", value)
        assert _env_bool("TEST_BOOL", False) is True


def test_env_int_uses_default_for_missing_blank_or_invalid(monkeypatch):
    monkeypatch.delenv("TEST_INT", raising=False)
    assert _env_int("TEST_INT", 8) == 8

    monkeypatch.setenv("TEST_INT", "")
    assert _env_int("TEST_INT", 8) == 8

    monkeypatch.setenv("TEST_INT", "not-a-number")
    assert _env_int("TEST_INT", 8) == 8


def test_env_int_parses_integer(monkeypatch):
    monkeypatch.setenv("TEST_INT", "42")
    assert _env_int("TEST_INT", 8) == 42
