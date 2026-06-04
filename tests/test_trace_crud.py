from crud.trace import _load_events


def test_load_events_returns_empty_for_invalid_json():
    assert _load_events("{bad-json") == []


def test_load_events_returns_empty_for_non_list_json():
    assert _load_events('{"event": 1}') == []


def test_load_events_keeps_only_dict_events():
    assert _load_events('[{"stage": "ok"}, "bad", 0, false, {"stage": "done"}]') == [
        {"stage": "ok"},
        {"stage": "done"},
    ]


def test_load_events_accepts_already_parsed_event_lists():
    assert _load_events([{"stage": "ok"}, "bad", None, {"stage": "done"}]) == [
        {"stage": "ok"},
        {"stage": "done"},
    ]
