import os

import httpx
from fastmcp import FastMCP

from . import tools, toolsets
from .client import ExactClient
from .config import load_settings, toolsets_from
from .tokens import BrokerTokens

INSTRUCTIONS = """\
Read-only access to one Exact Online connection: customers and suppliers, sales and purchase
invoices, open receivables and payables, general ledger and bank transactions, profit and loss,
revenue, VAT returns, sales orders, and items. Every tool takes an optional `division` (an Exact
administration); call list_divisions first when the user has more than one, and
financial_overview for a first look at how one is doing. Exact allows roughly 60 API calls a
minute per company, so prefer a narrow filter over paging through everything."""


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
    mcp = FastMCP("exact-online", instructions=INSTRUCTIONS)
    get_client = Connection().client
    tools.register(mcp, get_client)
    toolsets.register(mcp, get_client)
    # The agent sees the core tools, and only the toolsets EXACT_TOOLSETS names.
    mcp.enable(tags={"core", *toolsets_from(dict(os.environ))}, only=True)
    return mcp
