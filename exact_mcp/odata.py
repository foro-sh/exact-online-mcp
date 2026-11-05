import re
import uuid
from datetime import UTC, date, datetime
from xml.etree import ElementTree

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


def _local(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def entity_properties(edmx: str, entity_set: str) -> list[dict]:
    """The properties of `entity_set` (e.g. `Accounts`) in a `$metadata` document, as
    `{"name", "type"}`."""
    try:
        elements = list(ElementTree.fromstring(edmx).iter())
    except ElementTree.ParseError as err:
        raise ValueError("Exact's $metadata isn't valid XML.") from err
    sets = {
        element.get("Name", ""): element.get("EntityType", "") for element in elements if _local(element) == "EntitySet"
    }
    name = next((name for name in sets if name.lower() == entity_set.lower()), None)
    if name is None:
        raise ValueError(f"The service has no {entity_set}; it has {', '.join(sorted(sets)) or 'no resources'}.")
    type_name = sets[name].rsplit(".", 1)[-1]
    for element in elements:
        if _local(element) == "EntityType" and element.get("Name") == type_name:
            return [{"name": p.get("Name"), "type": p.get("Type")} for p in element if _local(p) == "Property"]
    raise ValueError(f"Exact's $metadata doesn't describe {name}'s type, {type_name}.")
