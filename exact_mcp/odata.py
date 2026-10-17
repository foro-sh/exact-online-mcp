import re
import uuid
from datetime import UTC, date, datetime

_DATE = re.compile(r"^/Date\((-?\d+)\)/$")


def string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def guid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError as err:
        raise ValueError(f"{value!r} is not an Exact Online ID (a GUID)") from err
    return f"guid'{parsed}'"


def datetime_literal(value: date) -> str:
    return f"datetime'{value.isoformat()}'"


def clean(record: dict) -> dict:
    """Drop OData metadata, turn `/Date(ms)/` into ISO dates, and trim the space padding
    Exact puts on codes."""
    out = {}
    for key, value in record.items():
        if key == "__metadata":
            continue
        if isinstance(value, str):
            match = _DATE.match(value)
            if match:
                stamp = datetime.fromtimestamp(int(match.group(1)) / 1000, UTC)
                value = stamp.date().isoformat() if stamp.time() == datetime.min.time() else stamp.isoformat()
            else:
                value = value.strip()
        out[key] = value
    return out
