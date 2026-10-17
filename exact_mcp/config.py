import base64
import os
from dataclasses import dataclass

# Exact runs a separate instance per country; a token only works on the host that issued it.
REGION_HOSTS = {
    "nl": "start.exactonline.nl",
    "be": "start.exactonline.be",
    "de": "start.exactonline.de",
    "uk": "start.exactonline.co.uk",
    "fr": "start.exactonline.fr",
    "es": "start.exactonline.es",
    "us": "start.exactonline.com",
}

REQUIRED = ("EXACT_CLIENT_ID", "EXACT_CLIENT_SECRET", "TOKEN_STORE_URL", "TOKEN_STORE_KEY")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    region: str
    division: int | None
    token_store_url: str
    token_store_key: bytes

    @property
    def base_url(self) -> str:
        return f"https://{REGION_HOSTS[self.region]}"

    @property
    def token_key(self) -> str:
        """Row key in the token store: one Exact connection per app per region."""
        return f"{self.region}:{self.client_id}"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise ConfigError(
            "Exact Online is not connected yet. Set these secrets in the foro dashboard and "
            f"redeploy: {', '.join(missing)}. See the README's setup section."
        )

    region = env.get("EXACT_REGION", "nl").lower()
    if region not in REGION_HOSTS:
        raise ConfigError(f"EXACT_REGION must be one of {', '.join(REGION_HOSTS)}, got {region!r}")

    division = env.get("EXACT_DIVISION")
    if division is not None and not division.isdigit():
        raise ConfigError(f"EXACT_DIVISION must be a division number, got {division!r}")

    try:
        key = base64.b64decode(env["TOKEN_STORE_KEY"], validate=True)
    except ValueError as err:
        raise ConfigError("TOKEN_STORE_KEY must be base64") from err
    if len(key) != 32:
        raise ConfigError("TOKEN_STORE_KEY must decode to 32 bytes - generate one with `connect keygen`")

    return Settings(
        client_id=env["EXACT_CLIENT_ID"],
        client_secret=env["EXACT_CLIENT_SECRET"],
        region=region,
        division=int(division) if division else None,
        token_store_url=env["TOKEN_STORE_URL"],
        token_store_key=key,
    )
