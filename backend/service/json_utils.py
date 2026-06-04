import json


def load_json_value(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, type(default)):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default
