"""Short-lived upload URLs, so a file reaches upload_document without being typed out as Base64.

create_upload hands out a URL; the agent PUTs the file to it from a shell, or a person drops it
on the page a GET of that URL serves. upload_document(upload_id) then stores it in Exact inside
a normal MCP request, which is the only place an Exact token for the caller can be had.

The URL is the capability: its token is 32 random bytes and only its hash is kept."""

import hashlib
import html
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .toolsets.documents import MAX_ATTACHMENT_BYTES

TTL = 15 * 60
# Expired uploads are kept this long without their bytes, to answer 410 rather than 404.
FORGET_AFTER = 60 * 60
MAX_TOTAL_BYTES = 50_000_000

# What Exact takes as an attachment here, told apart by the file's first bytes.
FILE_TYPES = (
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"<", "application/xml", ".xml"),
)


class UploadError(ValueError):
    pass


def file_type(data: bytes) -> tuple[str, str] | None:
    head = data.removeprefix(b"\xef\xbb\xbf").lstrip()
    for magic, content_type, extension in FILE_TYPES:
        if head.startswith(magic):
            return content_type, extension
    return None


@dataclass
class Upload:
    id: str
    caller: str | None
    expires: float
    file_name: str | None
    data: bytes | None = None
    content_type: str | None = None
    used: bool = False


class Uploads:
    # ponytail: in-memory, so the PUT and upload_document must reach the same replica; move to a
    # shared store if foro runs more than one replica without sticky routing.
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._by_hash: dict[str, Upload] = {}

    def create(self, caller: str | None, file_name: str | None) -> tuple[Upload, str]:
        self._sweep()
        token = secrets.token_urlsafe(32)
        upload = Upload("upl_" + secrets.token_hex(8), caller, self._clock() + TTL, file_name)
        self._by_hash[_hash(token)] = upload
        return upload, token

    def by_token(self, token: str) -> Upload | None:
        self._sweep()
        return self._by_hash.get(_hash(token))

    def expired(self, upload: Upload) -> bool:
        return self._clock() >= upload.expires

    def room_for(self, size: int) -> bool:
        return sum(len(u.data or b"") for u in self._by_hash.values()) + size <= MAX_TOTAL_BYTES

    def take(self, upload_id: str, caller: str | None) -> Upload:
        """The upload's file for upload_document, marked used; give it back with release()."""
        self._sweep()
        upload = next((u for u in self._by_hash.values() if u.id == upload_id), None)
        if upload is None or self.expired(upload):
            raise UploadError(f"Upload {upload_id} doesn't exist or has expired; call create_upload for a new one.")
        if upload.caller != caller:
            raise UploadError(f"Upload {upload_id} was made for another user.")
        if upload.used:
            raise UploadError(f"Upload {upload_id} was already stored in Exact; call create_upload for a new one.")
        if upload.data is None:
            raise UploadError(
                f"Nothing was uploaded to {upload_id} yet. PUT the file to its upload_url, or ask the user "
                "to open the link and drop the file there, then try again."
            )
        upload.used = True
        return upload

    def release(self, upload: Upload) -> None:
        upload.used = False

    def finish(self, upload: Upload) -> None:
        upload.data = None

    def _sweep(self) -> None:
        now = self._clock()
        for key, upload in list(self._by_hash.items()):
            if now >= upload.expires:
                upload.data = None
            if now >= upload.expires + FORGET_AFTER:
                del self._by_hash[key]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


STORE = Uploads()


def public_base() -> str:
    """This server's public URL, from the request the tool call came in on."""
    try:
        request = get_http_request()
    except RuntimeError as err:
        raise UploadError("Uploads only work when the server is reached over HTTP.") from err
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    proto = request.headers.get("x-forwarded-proto") or "https"
    return f"{proto}://{host}"


class _RedactUploads(logging.Filter):
    """The token is the capability, so keep it out of uvicorn's access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) > 2 and str(args[2]).startswith("/uploads/"):
            record.args = (*args[:2], "/uploads/<redacted>", *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactUploads())


def register_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/uploads/{token}", methods=["GET", "PUT", "POST"], include_in_schema=False)
    async def upload(request: Request) -> Response:
        store = STORE  # read per request, so a test can swap it
        found = store.by_token(request.path_params["token"])
        if request.method == "GET":
            return _page(found, store)
        if found is None:
            return _error(404, "No such upload.")
        if store.expired(found):
            return _error(410, "This upload link has expired; ask for a new one.")
        if found.data is not None or found.used:
            return _error(409, "A file was already uploaded to this link.")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_ATTACHMENT_BYTES:
            return _too_large()
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_ATTACHMENT_BYTES:
                return _too_large()
        if not data:
            return _error(400, "The file is empty.")
        kind = file_type(bytes(data))
        if kind is None:
            return _error(415, "Only PDF, PNG, JPEG and XML files can be uploaded.")
        if not store.room_for(len(data)):
            return _error(503, "Too many uploads are waiting right now; try again in a few minutes.")
        # Two PUTs may have streamed at once; the first to get here wins.
        if found.data is not None:
            return _error(409, "A file was already uploaded to this link.")
        found.data, found.content_type = bytes(data), kind[0]
        if name := request.headers.get("x-file-name"):
            found.file_name = _unquote(name)
        return JSONResponse(
            {
                "upload_id": found.id,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_type": kind[0],
            },
            headers=NO_STORE,
        )


def _unquote(name: str) -> str:
    return unquote(name).replace("/", "_").replace("\\", "_")[:200]


NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}
PAGE_HEADERS = {
    **NO_STORE,
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; form-action 'none'; frame-ancestors 'none'",
}


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=NO_STORE)


def _too_large() -> JSONResponse:
    return _error(413, f"The file is larger than {MAX_ATTACHMENT_BYTES:,} bytes.")


def expires_at(upload: Upload) -> str:
    return datetime.fromtimestamp(upload.expires, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _page(upload: Upload | None, store: Uploads) -> HTMLResponse:
    if upload is None or store.expired(upload):
        body = "<h1>This link has expired</h1><p>Ask your assistant for a new upload link.</p>"
    elif upload.data is not None or upload.used:
        body = "<h1>A file was already uploaded here</h1><p>You can tell your assistant it's done.</p>"
    else:
        expecting = f"<p>Expecting: <b>{html.escape(upload.file_name)}</b></p>" if upload.file_name else ""
        body = DROP.format(expecting=expecting, max_mb=MAX_ATTACHMENT_BYTES // 1_000_000)
    return HTMLResponse(PAGE.format(body=body), headers=PAGE_HEADERS)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Upload to Exact Online</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;color:#222;background:#fff}}
@media (prefers-color-scheme:dark){{body{{color:#eee;background:#161616}}}}
label{{display:block;border:2px dashed #888;border-radius:12px;padding:2.5rem 1rem;text-align:center;cursor:pointer}}
label.over{{border-color:#e4002b}}input{{display:none}}#status{{margin-top:1rem}}
</style></head><body>{body}</body></html>"""

DROP = """<h1>Upload a file to Exact Online</h1>{expecting}
<label id="drop">Drop a PDF, PNG, JPEG or XML file here, or click to pick one (at most {max_mb} MB).
<input id="file" type="file" accept=".pdf,.png,.jpg,.jpeg,.xml,application/pdf,image/png,image/jpeg,text/xml"></label>
<p id="status" role="status"></p>
<script>
const drop = document.getElementById("drop"), input = document.getElementById("file"),
  status = document.getElementById("status");
async function send(file) {{
  status.textContent = "Uploading " + file.name + "…";
  const r = await fetch(location.href, {{method: "PUT", body: file,
    headers: {{"X-File-Name": encodeURIComponent(file.name)}}}}).catch(() => null);
  const body = r ? await r.json().catch(() => ({{}})) : {{}};
  if (r && r.ok) {{
    drop.remove();
    status.textContent = "Uploaded " + file.name + ", " + Math.ceil(file.size / 1024) +
      " KB. You can tell your assistant it's done.";
  }} else {{
    status.textContent = "Upload failed: " + (body.error || "network error");
  }}
}}
input.onchange = () => input.files[0] && send(input.files[0]);
drop.ondragover = e => {{ e.preventDefault(); drop.classList.add("over"); }};
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = e => {{ e.preventDefault(); drop.classList.remove("over");
  e.dataTransfer.files[0] && send(e.dataTransfer.files[0]); }};
</script>"""
