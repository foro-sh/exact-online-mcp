"""One-time Exact Online authorisation: prints the upstream grant to add on the foro project.

uv run python -m exact_mcp.connect
"""

import asyncio
import os
import sys
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import GRANT_NAME, ConfigError, base_url, region_from
from .tokens import exchange_code, utcnow

# Exact refuses a refresh until the access token's last 30 seconds, so the first one foro makes
# must not come sooner. The grant it prints carries no access token for foro to wait out.
REFRESH_WINDOW = timedelta(seconds=30)


def authorize_url(base: str, client_id: str, redirect_uri: str) -> str:
    query = urlencode(
        {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "force_login": "0"}
    )
    return f"{base}/api/oauth2/auth?{query}"


def code_from(pasted: str) -> str:
    """The `code` from the URL Exact redirected to, or the pasted value itself if it is a bare code."""
    pasted = pasted.strip()
    if "://" not in pasted:
        return pasted
    codes = parse_qs(urlparse(pasted).query).get("code")
    if not codes:
        raise ValueError("That URL has no `code` parameter - copy the full address Exact redirected to.")
    return codes[0]


def prompt_for_code(base: str, client_id: str, redirect_uri: str) -> str:
    print("1. Open this URL, sign in to Exact Online and approve access:\n")
    print(f"   {authorize_url(base, client_id, redirect_uri)}\n")
    print("2. Exact redirects to your redirect URI. Paste that full address here")
    print("   (the page itself may fail to load - only the address matters).\n")
    return code_from(input("> "))


async def connect(base: str, client_id: str, client_secret: str, redirect_uri: str, code: str) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        tokens = await exchange_code(http, base, client_id, client_secret, code, redirect_uri)
        response = await http.get(
            f"{base}/api/v1/current/Me",
            params={"$select": "CurrentDivision,FullName"},
            headers={"Authorization": f"Bearer {tokens.access_token}", "Accept": "application/json"},
        )
        response.raise_for_status()
        me = response.json()["d"]["results"][0]
    print(f"\nConnected as {me['FullName']} (default division {me['CurrentDivision']}).")

    wait = (tokens.expires_at - REFRESH_WINDOW - utcnow()).total_seconds()
    if wait > 0:
        print(f"Waiting {wait / 60:.0f} minutes, until Exact will accept foro's first refresh...")
        await asyncio.sleep(wait)

    print("\n3. In the foro dashboard, open the project's Env tab and add an upstream grant:\n")
    print(f"   Name            {GRANT_NAME}")
    print(f"   Token endpoint  {base}/api/oauth2/token")
    print(f"   Client id       {client_id}")
    print("   Client secret   EXACT_CLIENT_SECRET")
    print(f"   Refresh token   {tokens.refresh_token}\n")
    print("The refresh token works once: foro spends it on its first refresh and keeps the next one.")


def main() -> None:
    names = ("EXACT_CLIENT_ID", "EXACT_CLIENT_SECRET", "EXACT_REDIRECT_URI")
    if missing := [name for name in names if not os.environ.get(name)]:
        sys.exit(f"Set {', '.join(missing)} (see the README).")
    try:
        base = base_url(region_from(dict(os.environ)))
    except ConfigError as err:
        sys.exit(str(err))
    client_id, redirect_uri = os.environ["EXACT_CLIENT_ID"], os.environ["EXACT_REDIRECT_URI"]
    code = prompt_for_code(base, client_id, redirect_uri)
    asyncio.run(connect(base, client_id, os.environ["EXACT_CLIENT_SECRET"], redirect_uri, code))


if __name__ == "__main__":
    main()
