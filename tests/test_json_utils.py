from service.json_utils import load_json_value


def test_load_json_value_keeps_expected_type():
    assert load_json_value('{"ok": true}', {}) == {"ok": True}
    assert load_json_value("[1, 2]", []) == [1, 2]


def test_load_json_value_rejects_unexpected_type():
    assert load_json_value('"not-a-dict"', {}) == {}
    assert load_json_value('{"not": "a-list"}', []) == []


def test_load_json_value_preserves_parsed_falsy_values():
    assert load_json_value(0, 1) == 0
    assert load_json_value(False, True) is False
