import base64
from pathlib import Path

import httpx
from fastmcp import FastMCP
from mcp.types import Icon

from . import tools, toolsets
from .client import ExactClient
from .config import load_settings
from .tokens import BrokerTokens

INSTRUCTIONS = """\
Access to one Exact Online connection: customers and suppliers, sales and purchase
invoices, open receivables and payables, general ledger and bank transactions, profit and loss,
revenue, VAT returns, sales orders, and items. It can also book purchase and sales invoices,
journal entries, new customers and suppliers, documents, and matches of open items. Every tool
takes an optional `division` (an Exact administration); call list_divisions first when the user
has more than one, and financial_overview for a first look at how one is doing. Exact allows
roughly 60 API calls a minute per company, so prefer a narrow filter over paging through
everything. Check a booking with dry_run first when anything about it is uncertain, give invoices
their expected_total, and never pass allow_duplicate to get past a refusal without asking the
user. The book_purchase_invoices and reconcile_open_items prompts walk through the usual work."""

# Sent in the initialize handshake, so clients show the logo without fetching a favicon. A data
# URI, because the server doesn't know which host it's deployed under.
_ICON_PNG = (Path(__file__).resolve().parent.parent / "icon.png").read_bytes()
ICON = Icon(src="data:image/png;base64," + base64.b64encode(_ICON_PNG).decode(), mime_type="image/png")


class Connection:
    """Built on the first tool call, not at startup, so a freshly templated server deploys and
    answers with setup instructions before it is connected."""

    def __init__(self) -> None:
        self._client: ExactClient | None = None

    async def client(self) -> ExactClient:
        if self._client is None:
            settings = load_settings()
            http = httpx.AsyncClient(timeout=30)
            self._client = ExactClient(http, BrokerTokens(http, settings), settings)
        return self._client


def create_server() -> FastMCP:
    mcp = FastMCP("exact-online", instructions=INSTRUCTIONS, icons=[ICON])
    get_client = Connection().client
    tools.register(mcp, get_client)
    toolsets.register(mcp, get_client)
    return mcp
