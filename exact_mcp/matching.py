"""Matching open items (an invoice against the payment or credit note that settles it), which
Exact's REST API can't do: it goes through the XML API's MatchSets topic."""

import re
from collections import defaultdict
from xml.etree import ElementTree

# A set is settled when its amounts add up to zero, to the cent.
TOLERANCE = 0.005
# eExact message types: 0 error, 1 warning, 2 success, 3 fatal.
FAILED_MESSAGES = {0, 3}
_TOKEN = re.compile(r"[A-Za-z0-9-]+")


def match_sets_xml(gl_account_code: str, account_code: str, lines: list[dict]) -> str:
    """One MatchSet: `lines` are `{"year", "period", "journal", "entry", "amount_dc"}`, a line per
    entry on the receivables or payables account, adding up to zero."""
    root = ElementTree.Element("eExact")
    match_set = ElementTree.SubElement(ElementTree.SubElement(root, "MatchSets"), "MatchSet")
    ElementTree.SubElement(match_set, "GLAccount", code=gl_account_code)
    ElementTree.SubElement(match_set, "Account", code=account_code)
    match_lines = ElementTree.SubElement(match_set, "MatchLines")
    for line in lines:
        ElementTree.SubElement(
            match_lines,
            "MatchLine",
            finyear=str(line["year"]),
            finperiod=str(line["period"]),
            journal=line["journal"],
            entry=str(line["entry"]),
            amountdc=f"{line['amount_dc']:.2f}",
        )
    return '<?xml version="1.0" encoding="utf-8"?>' + ElementTree.tostring(root, encoding="unicode")


def upload_messages(text: str) -> list[dict]:
    """The `{"type", "description"}` of each Message in an XML upload's answer."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as err:
        raise ValueError(f"Exact's XML API answered with something that isn't XML: {text[:200]!r}") from err
    return [
        {"type": int(message.get("type", "0")), "description": (message.findtext("Description") or "").strip()}
        for message in root.iter("Message")
    ]


def _references(item: dict) -> set[str]:
    refs = {str(item["InvoiceNumber"])} if item.get("InvoiceNumber") else set()
    if item.get("YourRef") and len(item["YourRef"]) >= 4:
        refs.add(item["YourRef"].lower())
    return refs


def _mentions(payment: dict, item: dict) -> bool:
    # A cashflow entry's InvoiceNumber is the invoice it was booked against.
    if payment.get("InvoiceNumber") and payment["InvoiceNumber"] == item.get("InvoiceNumber"):
        return True
    tokens = {token.lower() for token in _TOKEN.findall(payment.get("Description") or "")}
    return bool(_references(item) & tokens)


def _settles(items: list[dict]) -> bool:
    return abs(sum(item["Amount"] for item in items)) < TOLERANCE


def _suggestion(account_id: str, currency: str, reason: str, items: list[dict]) -> dict:
    return {
        "account_id": account_id,
        "account_name": items[0]["AccountName"],
        "currency": currency,
        "reason": reason,
        "items": items,
    }


def suggest_matches(open_items: list[dict]) -> dict:
    """Sets of open items that settle each other, from rows of ReceivablesList or PayablesList.
    Per account and currency, in order of how sure the match is:

    - `account_settled`: every open item of the account together adds up to zero.
    - `reference`: a payment booked against, or whose description names, the invoices it adds up to.
    - `amount`: a payment with exactly one invoice of the opposite amount.

    A payment with several invoices of its amount and no reference to one is left in `unmatched`,
    with them as `candidates`."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for item in open_items:
        groups[(item["AccountId"], item["CurrencyCode"])].append(item)

    suggestions, unmatched = [], []
    for (account_id, currency), items in groups.items():
        found = []
        if len(items) > 1 and _settles(items):
            found.append(("account_settled", items))
        else:
            # ReceivablesList and PayablesList both show what is owed as positive, so an invoice is
            # positive and a payment or credit note negative, on either side (seen in a live company).
            invoices = [item for item in items if item["Amount"] > 0]
            payments = [item for item in items if item["Amount"] < 0]
            used: set[int] = set()
            for payment in sorted(payments, key=lambda p: abs(p["Amount"]), reverse=True):
                free = [item for item in invoices if id(item) not in used]
                named = [item for item in free if _mentions(payment, item)]
                if named and _settles([payment, *named]):
                    found.append(("reference", [payment, *named]))
                    used |= {id(item) for item in named}
                    continue
                same = [item for item in free if abs(item["Amount"] + payment["Amount"]) < TOLERANCE]
                if len(same) == 1:
                    found.append(("amount", [payment, same[0]]))
                    used.add(id(same[0]))
                else:
                    unmatched.append({**payment, "candidates": same})
        suggestions.extend(_suggestion(account_id, currency, reason, members) for reason, members in found)
    return {"suggestions": suggestions, "unmatched": unmatched}
