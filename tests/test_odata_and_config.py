import base64
from datetime import date

import pytest

from exact_mcp import odata
from exact_mcp.config import ConfigError, load_settings

from .conftest import ENV


def test_clean_converts_dates_strips_padding_and_drops_metadata():
    record = {
        "__metadata": {"uri": "x"},
        "Code": "                20",
        "InvoiceDate": "/Date(1760659200000)/",
        "Modified": "/Date(1760695815123)/",
        "Amount": 12.5,
        "Empty": None,
    }
    assert odata.clean(record) == {
        "Code": "20",
        "InvoiceDate": "2025-10-17",
        "Modified": "2025-10-17T10:10:15.123000+00:00",
        "Amount": 12.5,
        "Empty": None,
    }


def test_literals():
    assert odata.string("O'Brien") == "'O''Brien'"
    assert odata.guid("8F7B2C1E-0000-4000-8000-000000000001") == "guid'8f7b2c1e-0000-4000-8000-000000000001'"
    assert odata.datetime_literal(date(2026, 1, 31)) == "datetime'2026-01-31'"
    with pytest.raises(ValueError, match="not an Exact Online ID"):
        odata.guid("1 eq 1) or (ID")


def test_settings_defaults_to_nl_and_current_division():
    settings = load_settings(ENV)
    assert settings.base_url == "https://start.exactonline.nl"
    assert settings.division is None
    assert settings.token_key == "nl:client-id"


def test_missing_secrets_are_named():
    with pytest.raises(ConfigError, match="EXACT_CLIENT_SECRET, TOKEN_STORE_KEY"):
        load_settings({"EXACT_CLIENT_ID": "x", "TOKEN_STORE_URL": "y"})


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"EXACT_REGION": "us-east"}, "EXACT_REGION"),
        ({"EXACT_DIVISION": "abc"}, "EXACT_DIVISION"),
        ({"TOKEN_STORE_KEY": "not base64!"}, "base64"),
        ({"TOKEN_STORE_KEY": base64.b64encode(b"short").decode()}, "32 bytes"),
    ],
)
def test_invalid_settings_are_rejected(override, message):
    with pytest.raises(ConfigError, match=message):
        load_settings({**ENV, **override})


def test_uk_region_host():
    assert load_settings({**ENV, "EXACT_REGION": "UK"}).base_url == "https://start.exactonline.co.uk"
