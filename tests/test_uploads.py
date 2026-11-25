import base64

import httpx
import pytest
from fastmcp.exceptions import ToolError

from exact_mcp import uploads

from .test_bookkeeping import DOCUMENT_ID, Books, server  # noqa: F401
from .test_tools import call

PDF = b"%PDF-1.7 receipt"


@pytest.fixture(autouse=True)
def store(monkeypatch):
    now = [1_800_000_000.0]
    fresh = uploads.Uploads(clock=lambda: now[0])
    fresh.now = now
    monkeypatch.setattr(uploads, "STORE", fresh)
    monkeypatch.setattr(uploads, "public_base", lambda: "https://exact.foro.sh")
    return fresh


@pytest.fixture
def http(server):  # noqa: F811
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.http_app()), base_url="https://exact.foro.sh")


def path(created: dict) -> str:
    return created["upload_url"].removeprefix("https://exact.foro.sh")


async def test_a_file_put_to_the_upload_url_is_stored_as_a_document(server, exact, http):  # noqa: F811
    exact.handler = books = Books({})
    created = await call(server, "create_upload", {"file_name": "inv.pdf"})
    assert created["curl"] == f"curl --fail -T inv.pdf '{created['upload_url']}'"
    assert created["max_bytes"] == 10_000_000

    put = await http.put(path(created), content=PDF)
    assert put.status_code == 200
    assert put.json()["content_type"] == "application/pdf"
    result = await call(server, "upload_document", {"upload_id": created["upload_id"]})

    assert result["bytes"] == len(PDF)
    assert books.posted == [
        ("Documents", {"Subject": "inv.pdf", "Type": 20}),
        (
            "DocumentAttachments",
            {"Document": DOCUMENT_ID, "FileName": "inv.pdf", "Attachment": base64.b64encode(PDF).decode()},
        ),
    ]
    with pytest.raises(ToolError, match="already stored"):
        await call(server, "upload_document", {"upload_id": created["upload_id"]})
    assert (await http.put(path(created), content=PDF)).status_code == 409


async def test_the_upload_url_refuses_what_it_cant_take(server, http, store, monkeypatch):  # noqa: F811
    created = await call(server, "create_upload")
    assert (await http.put(path(created), content=b"MZ\x90 an exe")).status_code == 415
    monkeypatch.setattr(uploads, "MAX_ATTACHMENT_BYTES", 10)
    assert (await http.put(path(created), content=PDF)).status_code == 413

    async def chunked():  # no Content-Length, so the cap must hold while streaming
        yield PDF

    assert (await http.put(path(created), content=chunked())).status_code == 413
    assert (await http.put("/uploads/nonsense", content=PDF)).status_code == 404
    store.now[0] += uploads.TTL
    assert (await http.put(path(created), content=PDF)).status_code == 410
    with pytest.raises(ToolError, match="expired"):
        await call(server, "upload_document", {"upload_id": created["upload_id"]})


async def test_an_upload_is_only_for_the_user_who_made_it(server, http, caller):  # noqa: F811
    caller("alice")
    created = await call(server, "create_upload")
    await http.put(path(created), content=PDF)
    caller("bob")
    with pytest.raises(ToolError, match="another user"):
        await call(server, "upload_document", {"upload_id": created["upload_id"]})


async def test_upload_document_says_when_nothing_was_uploaded_yet(server):  # noqa: F811
    created = await call(server, "create_upload")
    with pytest.raises(ToolError, match="Nothing was uploaded"):
        await call(server, "upload_document", {"upload_id": created["upload_id"]})


@pytest.mark.parametrize("args", [{}, {"upload_id": "upl_x", "content_base64": "JVBERg==", "file_name": "a.pdf"}])
async def test_upload_document_takes_exactly_one_source(server, args):  # noqa: F811
    with pytest.raises(ToolError, match="either upload_id or content_base64"):
        await call(server, "upload_document", args)


async def test_the_upload_url_serves_a_drop_page_with_strict_headers(server, exact, http):  # noqa: F811
    exact.handler = books = Books({})
    created = await call(server, "create_upload", {"file_name": "factuur-<nov>.pdf"})

    page = await http.get(path(created))
    assert "Expecting: <b>factuur-&lt;nov&gt;.pdf</b>" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-content-type-options"] == "nosniff"

    # What the page's script sends: the dropped file's own name, which upload_document then uses.
    await http.put(path(created), content=PDF, headers={"x-file-name": "scan%201.pdf"})
    await call(server, "upload_document", {"upload_id": created["upload_id"]})
    assert books.posted[1][1]["FileName"] == "scan 1.pdf"
    assert "already uploaded" in (await http.get(path(created))).text


async def test_an_expired_link_shows_no_form(server, http, store):  # noqa: F811
    created = await call(server, "create_upload")
    store.now[0] += uploads.TTL
    page = await http.get(path(created))
    assert "expired" in page.text and "<input" not in page.text


def test_file_types_are_told_apart_by_their_first_bytes():
    assert uploads.file_type(b"\xef\xbb\xbf  <?xml version") == ("application/xml", ".xml")
    assert uploads.file_type(b"\x89PNG\r\n\x1a\n...") == ("image/png", ".png")
    assert uploads.file_type(b"\xff\xd8\xff\xe0") == ("image/jpeg", ".jpg")
    assert uploads.file_type(b"PK\x03\x04") is None
