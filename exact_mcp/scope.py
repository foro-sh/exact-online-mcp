"""What the generic query tool may read: no payroll, HR, user or mailbox records, and no field
that identifies a person or holds a file, whichever endpoint it comes from."""

import re

DENIED_SERVICES = {"payroll", "hrm", "users", "mailbox", "accountancy", "activities", "system", "webhooks", "workflow"}
# Cost centers and units live under hrm but are about the books, not people.
ALLOWED_HRM = {"costcenters", "costunits"}
DENIED_FIELDS = {
    "socialsecuritynumber",
    "birthdate",
    "birthname",
    "birthplace",
    "identificationdocument",
    "gender",
    "nationality",
    "picture",
    "pictureurl",
    "creator",
    "creatorfullname",
    "modifier",
    "modifierfullname",
    "deletedby",
    "attachment",
}
# read/, bulk/ and sync/ come before the service they read from.
_PREFIXES = ("read", "bulk", "sync")
_RESOURCE = re.compile(r"[A-Za-z]+(/[A-Za-z]+){1,3}")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING = re.compile(r"'(?:[^']|'')*'")


def resource_parts(resource: str) -> tuple[str, str]:
    """The service and entity of a resource query may read, such as `("read/financial",
    "AgingOverview")`, or a ValueError saying why it may not."""
    path = resource.strip().strip("/")
    if not _RESOURCE.fullmatch(path):
        raise ValueError(f"{resource!r} isn't a resource such as crm/Accounts or read/financial/AgingOverview.")
    parts = path.split("/")
    inner = [part.lower() for part in (parts[1:] if parts[0].lower() in _PREFIXES else parts)]
    if len(inner) < 2:
        raise ValueError(f"query can't read {path}; it needs a service and a resource, such as crm/Accounts.")
    if inner[0] in DENIED_SERVICES and not (inner[0] == "hrm" and inner[-1] in ALLOWED_HRM):
        raise ValueError(f"query doesn't read {inner[0]} resources, which hold records about people or users.")
    return "/".join(parts[:-1]), parts[-1]


def check_fields(select: list[str], *expressions: str | None) -> None:
    """Raise a ValueError if `select`, or a $filter or $orderby expression, names a field query
    doesn't read. Field names inside quoted strings don't count."""
    if bad := [name for name in select if not _NAME.fullmatch(name.strip())]:
        raise ValueError(f"Each select entry must be one field name, got {', '.join(map(repr, bad))}.")
    names = {name.strip().lower() for name in select}
    for expression in expressions:
        if expression:
            names |= {name.lower() for name in _NAME.findall(_STRING.sub("''", expression))}
    if denied := names & DENIED_FIELDS:
        raise ValueError(f"query doesn't read {', '.join(sorted(denied))}: they identify people or hold files.")
