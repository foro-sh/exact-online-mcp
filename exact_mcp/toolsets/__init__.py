"""Tools beyond the core ones, in groups that EXACT_TOOLSETS switches on (see the README)."""

from collections.abc import Awaitable, Callable

from fastmcp import FastMCP

from ..client import ExactClient
from ..tools import read_only_tools
from . import changes, crm, documents, orders, projects, subscriptions

MODULES = {
    "orders": orders,
    "projects": projects,
    "crm": crm,
    "subscriptions": subscriptions,
    "documents": documents,
    "changes": changes,
}


def register(mcp: FastMCP, get_client: Callable[[], Awaitable[ExactClient]]) -> None:
    for name, module in MODULES.items():
        module.register(read_only_tools(mcp, name), get_client)
