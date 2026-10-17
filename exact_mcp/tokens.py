import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
import psycopg
from nacl.secret import SecretBox

from .config import Settings

# Exact only accepts a refresh in the last 30 seconds of an access token's 10-minute life,
# so refreshing any earlier than this is refused rather than merely wasteful.
REFRESH_MARGIN = timedelta(seconds=30)


class NotConnectedError(Exception):
    pass


class TokenRefreshError(Exception):
    pass


@dataclass(frozen=True)
class TokenSet:
    refresh_token: str
    access_token: str | None = None
    expires_at: datetime | None = None

    def usable(self, now: datetime) -> bool:
        return self.access_token is not None and self.expires_at is not None and self.expires_at - now > REFRESH_MARGIN

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "refresh_token": self.refresh_token,
                "access_token": self.access_token,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            }
        ).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "TokenSet":
        data = json.loads(raw)
        expires_at = data["expires_at"]
        return cls(
            refresh_token=data["refresh_token"],
            access_token=data["access_token"],
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
        )


class TokenSlot:
    """The stored token set while its row is locked; assign `tokens` to write it back."""

    def __init__(self, tokens: TokenSet):
        self.tokens = tokens
        self.original = tokens

    @property
    def changed(self) -> bool:
        return self.tokens is not self.original


class TokenStore(Protocol):
    def locked(self) -> AbstractAsyncContextManager[TokenSlot]: ...

    async def put(self, tokens: TokenSet) -> None: ...


class PostgresTokenStore:
    """Tokens in a database the user brings, because a foro container has no disk that
    survives a restart and Exact rotates the refresh token on every use. The row lock
    serialises refreshes across replicas: a second refresh with the same token fails."""

    def __init__(self, url: str, key: bytes, row_key: str):
        self._url = url
        self._box = SecretBox(key)
        self._row_key = row_key

    async def init(self) -> None:
        async with await psycopg.AsyncConnection.connect(self._url) as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS exact_online_tokens ("
                " key text PRIMARY KEY,"
                " tokens bytea NOT NULL,"
                " updated_at timestamptz NOT NULL DEFAULT now())"
            )

    @asynccontextmanager
    async def locked(self) -> AsyncIterator[TokenSlot]:
        async with await psycopg.AsyncConnection.connect(self._url) as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT tokens FROM exact_online_tokens WHERE key = %s FOR UPDATE",
                    (self._row_key,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise NotConnectedError(
                        "No Exact Online connection in the token store yet - run "
                        "`uv run python -m exact_mcp.connect` once (see the README)."
                    )
                slot = TokenSlot(TokenSet.from_json(self._box.decrypt(bytes(row[0]))))
                yield slot
                if slot.changed:
                    await conn.execute(
                        "UPDATE exact_online_tokens SET tokens = %s, updated_at = now() WHERE key = %s",
                        (self._box.encrypt(slot.tokens.to_json()), self._row_key),
                    )

    async def put(self, tokens: TokenSet) -> None:
        async with await psycopg.AsyncConnection.connect(self._url) as conn:
            await conn.execute(
                "INSERT INTO exact_online_tokens (key, tokens) VALUES (%s, %s)"
                " ON CONFLICT (key) DO UPDATE SET tokens = EXCLUDED.tokens, updated_at = now()",
                (self._row_key, self._box.encrypt(tokens.to_json())),
            )


def utcnow() -> datetime:
    return datetime.now(UTC)


def _token_set(payload: dict, fallback_refresh_token: str | None, now: datetime) -> TokenSet:
    # Exact sends expires_in as a string.
    return TokenSet(
        refresh_token=payload.get("refresh_token") or fallback_refresh_token,
        access_token=payload["access_token"],
        expires_at=now + timedelta(seconds=int(payload["expires_in"])),
    )


async def _token_request(http: httpx.AsyncClient, settings: Settings, form: dict) -> dict:
    response = await http.post(
        f"{settings.base_url}/api/oauth2/token",
        data={**form, "client_id": settings.client_id, "client_secret": settings.client_secret},
    )
    if response.status_code != 200:
        raise TokenRefreshError(
            f"Exact rejected the token request ({response.status_code}: {response.text[:200]}). "
            "If this keeps happening, reconnect with `uv run python -m exact_mcp.connect`."
        )
    return response.json()


async def exchange_code(http: httpx.AsyncClient, settings: Settings, code: str, redirect_uri: str) -> TokenSet:
    payload = await _token_request(
        http,
        settings,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
    )
    return _token_set(payload, None, utcnow())


class TokenManager:
    def __init__(
        self,
        store: TokenStore,
        http: httpx.AsyncClient,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ):
        self._store = store
        self._http = http
        self._settings = settings
        self._clock = clock
        self._cached: TokenSet | None = None
        self._lock = asyncio.Lock()

    async def access_token(self) -> str:
        if self._cached and self._cached.usable(self._clock()):
            return self._cached.access_token
        async with self._lock:
            if not (self._cached and self._cached.usable(self._clock())):
                self._cached = await self._load_or_refresh()
        return self._cached.access_token

    def invalidate(self) -> None:
        self._cached = None

    async def _load_or_refresh(self) -> TokenSet:
        async with self._store.locked() as slot:
            # Another replica may have refreshed while this one waited on the lock.
            if not slot.tokens.usable(self._clock()):
                payload = await _token_request(
                    self._http,
                    self._settings,
                    {"grant_type": "refresh_token", "refresh_token": slot.tokens.refresh_token},
                )
                slot.tokens = _token_set(payload, slot.tokens.refresh_token, self._clock())
            return slot.tokens
