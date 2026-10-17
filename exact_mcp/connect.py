"""One-time Exact Online authorisation: seeds the token store the server refreshes from.

uv run python -m exact_mcp.connect keygen   # print a TOKEN_STORE_KEY
uv run python -m exact_mcp.connect          # authorise and store the first tokens
"""

import argparse
import asyncio
import base64
import os
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import nacl.utils
from nacl.secret import SecretBox

from .client import ExactClient
from .config import ConfigError, Settings, load_settings
from .tokens import PostgresTokenStore, TokenManager, exchange_code


def authorize_url(settings: Settings, redirect_uri: str) -> str:
    query = urlencode(
        {"client_id": settings.client_id, "redirect_uri": redirect_uri, "response_type": "code", "force_login": "0"}
    )
    return f"{settings.base_url}/api/oauth2/auth?{query}"


def code_from(pasted: str) -> str:
    """The `code` from the URL Exact redirected to, or the pasted value itself if it is a bare code."""
    pasted = pasted.strip()
    if "://" not in pasted:
        return pasted
    codes = parse_qs(urlparse(pasted).query).get("code")
    if not codes:
        raise ValueError("That URL has no `code` parameter - copy the full address Exact redirected to.")
    return codes[0]


def prompt_for_code(settings: Settings, redirect_uri: str) -> str:
    print("1. Open this URL, sign in to Exact Online and approve access:\n")
    print(f"   {authorize_url(settings, redirect_uri)}\n")
    print("2. Exact redirects to your redirect URI. Paste that full address here")
    print("   (the page itself may fail to load - only the address matters).\n")
    return code_from(input("> "))


async def connect(settings: Settings, redirect_uri: str, code: str) -> None:
    store = PostgresTokenStore(settings.token_store_url, settings.token_store_key, settings.token_key)
    await store.init()
    async with httpx.AsyncClient(timeout=30) as http:
        await store.put(await exchange_code(http, settings, code, redirect_uri))
        me = await ExactClient(http, TokenManager(store, http, settings), settings).me()
    print(f"\nConnected as {me['FullName']} (default division {me['CurrentDivision']}).")
    print("Tokens are stored encrypted; the deployed server refreshes them from here on.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m exact_mcp.connect", description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", choices=["keygen"], help="print a new TOKEN_STORE_KEY and exit")
    args = parser.parse_args()

    if args.command == "keygen":
        print(base64.b64encode(nacl.utils.random(SecretBox.KEY_SIZE)).decode())
        return

    try:
        settings = load_settings()
        redirect_uri = os.environ["EXACT_REDIRECT_URI"]
    except KeyError:
        sys.exit("Set EXACT_REDIRECT_URI to the redirect URI registered on your Exact app.")
    except ConfigError as err:
        sys.exit(str(err))
    asyncio.run(connect(settings, redirect_uri, prompt_for_code(settings, redirect_uri)))


if __name__ == "__main__":
    main()
