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

# The upstream grant's name on the foro project, which the broker looks the tokens up by.
GRANT_NAME = "exact"

# foro injects both into every container it deploys.
REQUIRED = ("FORO_UPSTREAM_BROKER_URL", "FORO_UPSTREAM_BROKER_TOKEN")


class ConfigError(Exception):
    pass


def base_url(region: str) -> str:
    return f"https://{REGION_HOSTS[region]}"


def region_from(env: dict[str, str]) -> str:
    region = env.get("EXACT_REGION", "nl").lower()
    if region not in REGION_HOSTS:
        raise ConfigError(f"EXACT_REGION must be one of {', '.join(REGION_HOSTS)}, got {region!r}")
    return region


@dataclass(frozen=True)
class Settings:
    region: str
    division: int | None
    broker_url: str
    broker_token: str

    @property
    def base_url(self) -> str:
        return base_url(self.region)


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise ConfigError(
            "This server gets its Exact Online tokens from foro's upstream broker, which only exists "
            f"inside a foro deploy. Missing: {', '.join(missing)}."
        )

    division = env.get("EXACT_DIVISION")
    if division is not None and not division.isdigit():
        raise ConfigError(f"EXACT_DIVISION must be a division number, got {division!r}")

    return Settings(
        region=region_from(env),
        division=int(division) if division else None,
        broker_url=env["FORO_UPSTREAM_BROKER_URL"].rstrip("/"),
        broker_token=env["FORO_UPSTREAM_BROKER_TOKEN"],
    )
