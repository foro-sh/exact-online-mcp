from datetime import date

import pytest

from exact_mcp import odata
from exact_mcp.config import TOOLSETS, ConfigError, load_settings, toolsets_from

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
    assert settings.broker_url == "http://worker:3005"


def test_missing_secrets_are_named():
    with pytest.raises(ConfigError, match="inside a foro deploy. Missing: FORO_UPSTREAM_BROKER_TOKEN"):
        load_settings({"FORO_UPSTREAM_BROKER_URL": "http://worker:3005"})


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"EXACT_REGION": "us-east"}, "EXACT_REGION"),
        ({"EXACT_DIVISION": "abc"}, "EXACT_DIVISION"),
    ],
)
def test_invalid_settings_are_rejected(override, message):
    with pytest.raises(ConfigError, match=message):
        load_settings({**ENV, **override})


def test_uk_region_host():
    assert load_settings({**ENV, "EXACT_REGION": "UK"}).base_url == "https://start.exactonline.co.uk"


def test_toolsets_are_off_by_default_and_all_switches_every_one_on():
    assert toolsets_from({}) == set()
    assert toolsets_from({"EXACT_TOOLSETS": " Orders, crm ,"}) == {"orders", "crm"}
    assert toolsets_from({"EXACT_TOOLSETS": "all"}) == set(TOOLSETS)


def test_an_unknown_toolset_is_named():
    with pytest.raises(ConfigError, match="EXACT_TOOLSETS can name .* got payroll"):
        toolsets_from({"EXACT_TOOLSETS": "orders,payroll"})


EDMX = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema Namespace="Exact.Web.Api.Models" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
      <EntityType Name="Address">
        <Key><PropertyRef Name="ID"/></Key>
        <Property Name="ID" Type="Edm.Guid" Nullable="false"/>
        <Property Name="City" Type="Edm.String"/>
      </EntityType>
      <EntityContainer Name="Container" m:IsDefaultEntityContainer="true">
        <EntitySet Name="Addresses" EntityType="Exact.Web.Api.Models.Address"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""


def test_entity_properties_reads_the_type_behind_an_entity_set():
    assert odata.entity_properties(EDMX, "addresses") == [
        {"name": "ID", "type": "Edm.Guid"},
        {"name": "City", "type": "Edm.String"},
    ]
    with pytest.raises(ValueError, match="no Accounts; it has Addresses"):
        odata.entity_properties(EDMX, "Accounts")
    with pytest.raises(ValueError, match="isn't valid XML"):
        odata.entity_properties("<nope", "Addresses")
