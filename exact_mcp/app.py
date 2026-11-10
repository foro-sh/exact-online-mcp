import os

import httpx
from fastmcp import FastMCP

from . import tools, toolsets
from .client import ExactClient
from .config import load_settings, toolsets_from
from .tokens import BrokerTokens

INSTRUCTIONS = """\
Access to one Exact Online connection: customers and suppliers, sales and purchase
invoices, open receivables and payables, general ledger and bank transactions, profit and loss,
revenue, VAT returns, sales orders, and items. Every tool takes an optional `division` (an Exact
administration); call list_divisions first when the user has more than one, and
financial_overview for a first look at how one is doing. Exact allows roughly 60 API calls a
minute per company, so prefer a narrow filter over paging through everything."""

BOOKKEEPING = """

The bookkeeping toolset is on, so this connection can also book: purchase and sales invoices,
journal entries, new customers and suppliers, documents, and matches of open items. Check a
booking with dry_run first when anything about it is uncertain, give invoices their
expected_total, and never pass allow_duplicate to get past a refusal without asking the user. The
book_purchase_invoices and reconcile_open_items prompts walk through the usual work."""


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
    enabled = toolsets_from(dict(os.environ))
    instructions = INSTRUCTIONS + (BOOKKEEPING if "bookkeeping" in enabled else "")
    mcp = FastMCP("exact-online", instructions=instructions)
    get_client = Connection().client
    tools.register(mcp, get_client)
    toolsets.register(mcp, get_client)
    # The agent sees the core tools, and only the toolsets EXACT_TOOLSETS names.
    mcp.enable(tags={"core", *enabled}, only=True)
    return mcp
