"""Tools beyond the core ones. All of them are listed; a foro project policy decides which an
agent may call. Most need a module that only some Exact packages have."""

from collections.abc import Awaitable, Callable

from fastmcp import FastMCP

from ..client import ExactClient
from ..tools import toolset_tools
from . import bookkeeping, changes, crm, documents, orders, projects, query, subscriptions

MODULES = {
    "orders": orders,
    "projects": projects,
    "crm": crm,
    "subscriptions": subscriptions,
    "documents": documents,
    "changes": changes,
    "query": query,
    "bookkeeping": bookkeeping,
}


def register(mcp: FastMCP, get_client: Callable[[], Awaitable[ExactClient]]) -> None:
    for name, module in MODULES.items():
        module.register(toolset_tools(mcp, name), get_client)
    bookkeeping.register_prompts(mcp)
