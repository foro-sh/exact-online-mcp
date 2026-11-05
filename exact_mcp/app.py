import httpx
from fastmcp import FastMCP

from . import tools
from .client import ExactClient
from .config import load_settings
from .tokens import BrokerTokens

INSTRUCTIONS = """\
Read-only access to one Exact Online connection: customers and suppliers, open receivables and
payables, sales invoices and orders, items, and profit and loss. Every tool takes an optional
`division` (an Exact administration); call list_divisions first when the user has more than one.
Exact allows roughly 60 API calls a minute per company, so prefer a narrow filter over paging
through everything."""


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
    tools.register(mcp, Connection().client)
    return mcp
