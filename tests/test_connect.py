from urllib.parse import parse_qs, urlparse

import pytest

from exact_mcp.connect import authorize_url, code_from


def test_authorize_url_targets_the_region_host():
    url = urlparse(authorize_url("https://start.exactonline.nl", "client-id", "https://example.com/callback"))
    assert (url.netloc, url.path) == ("start.exactonline.nl", "/api/oauth2/auth")
    assert parse_qs(url.query) == {
        "client_id": ["client-id"],
        "redirect_uri": ["https://example.com/callback"],
        "response_type": ["code"],
        "force_login": ["0"],
    }


def test_code_is_taken_from_a_pasted_redirect_url_and_decoded():
    assert code_from(" https://example.com/callback?code=abc%21def&state=x \n") == "abc!def"


def test_bare_code_is_accepted():
    assert code_from("abc!def") == "abc!def"


def test_url_without_code_is_rejected():
    with pytest.raises(ValueError, match="no `code`"):
        code_from("https://example.com/callback?error=access_denied")
