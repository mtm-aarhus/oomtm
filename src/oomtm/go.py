"""GO (Aarhus Kommune ESDH) helpers for MTM OO processes.

GO authenticates via NTLM. The lib provides:

* ``session()`` — preconfigured NTLM ``requests.Session``
* ``fetch_metadata()`` — pulls DocumentType + UIVersionString from GO's
  ``Documents/Data/{id}`` endpoint
* ``download_file()`` — chunked-safe download. GO's ``DocumentBytes`` endpoint
  silently truncates large files (typically anything above ~10 MB), so this
  function falls back to extracting the underlying SharePoint blob URL from
  ``MetadataWithSystemFields/{id}.ows_EncodedAbsUrl`` and streams that. This
  hack is exactly the one used by the legacy
  ``Python_AktBob2-GenerererAktindsigter/PrepareEachDocumentToUpload.download_file``.
* ``pdf_convert()`` — calls GO's built-in PDF converter
  (``Documents/ConvertToPDF/{id}/{version}``) and returns the bytes, or None
  if GO declines to convert the file.

All functions are stateless; the caller manages the session lifetime.
"""
from __future__ import annotations

import json
import re
import time

import requests
from requests_ntlm import HttpNtlmAuth


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def session(username: str, password: str) -> requests.Session:
    """Return a fresh NTLM-authenticated session for GO."""
    s = requests.Session()
    s.auth = HttpNtlmAuth(username, password)
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


_RE_FILE_TYPE = re.compile(r'ows_File_x0020_Type="([^"]+)"')
_RE_VERSION_UI = re.compile(r'ows__UIVersionString="([^"]+)"')


def fetch_metadata(s: requests.Session, *, base_url: str, dok_id: str) -> dict:
    """Fetch a document's metadata from GO.

    Returns a dict with at least:

    * ``ext`` — bare file extension (e.g. ``"docx"``, ``"pdf"``, ``"goref"``)
    * ``version_ui`` — UI version string needed by GO's PDF converter

    Raises ``requests.HTTPError`` if GO refuses or times out.
    """
    url = f"{base_url}/_goapi/Documents/Data/{dok_id}"
    r = s.get(url, timeout=60)
    r.raise_for_status()
    data = json.loads(r.text)
    item_props = data.get("ItemProperties", "") or ""

    ext_m = _RE_FILE_TYPE.search(item_props)
    ver_m = _RE_VERSION_UI.search(item_props)
    return {
        "ext": ext_m.group(1) if ext_m else None,
        "version_ui": ver_m.group(1) if ver_m else None,
        "raw": data,
    }


def fetch_parents(s: requests.Session, *, base_url: str, dok_id: str) -> list[str]:
    """Return list of parent DocumentIds (bilag-til relationships)."""
    url = f"{base_url}/_goapi/Documents/Parents/{dok_id}"
    try:
        r = s.get(url, timeout=60)
        r.raise_for_status()
        return [str(it.get("DocumentId") or "") for it in (r.json().get("ParentsData") or [])]
    except requests.RequestException:
        return []


def fetch_children(s: requests.Session, *, base_url: str, dok_id: str) -> list[str]:
    """Return list of child DocumentIds (bilag-relationships)."""
    url = f"{base_url}/_goapi/Documents/Children/{dok_id}"
    try:
        r = s.get(url, timeout=60)
        r.raise_for_status()
        return [str(it.get("DocumentId") or "") for it in (r.json().get("ChildrenData") or [])]
    except requests.RequestException:
        return []


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download_file(
    s: requests.Session,
    *,
    base_url: str,
    dok_id: str,
    local_path: str,
    max_retries: int = 30,
    retry_interval: int = 5,
    chunk_size: int = 8192,
) -> None:
    """Download a document from GO and write it to ``local_path``.

    Strategy:

    1. Try ``/Documents/DocumentBytes/{id}`` (fast, but silently truncates
       large files into an HTML 503 page). Retry up to ``max_retries`` times.
    2. If that fails or returns nonsense, fall back to fetching
       ``/Documents/MetadataWithSystemFields/{id}``, extracting the
       ``ows_EncodedAbsUrl`` (the underlying SharePoint blob URL), and
       streaming from there in ``chunk_size`` chunks.

    Step 2 is the only way to reliably pull files above ~10 MB from GO.
    Raises ``RuntimeError`` if both paths fail.
    """
    # --- Primary path: /DocumentBytes ---
    url = f"{base_url}/_goapi/Documents/DocumentBytes/{dok_id}"
    last_err: Exception | None = None

    for attempt in range(max_retries):
        try:
            r = s.get(url, timeout=180)
            r.raise_for_status()
            if r.status_code == 200:
                content = r.content
                # GO returns an HTML 503 page disguised as 200 when the file
                # is too big for the binary endpoint.
                if b"HTTP Error 503. The service is unavailable." in content:
                    last_err = RuntimeError("GO returned 503 page in 200 body (likely oversize)")
                    break  # fall straight to SP-blob fallback; retrying won't help
                with open(local_path, "wb") as fh:
                    fh.write(content)
                return
        except Exception as exc:  # pylint: disable=broad-except
            last_err = exc
        if attempt < max_retries - 1:
            time.sleep(retry_interval)

    # --- Fallback path: extract SP-blob URL from metadata ---
    meta_url = f"{base_url}/_goapi/Documents/MetadataWithSystemFields/{dok_id}"
    try:
        r = s.get(meta_url, timeout=60)
        r.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            f"GO download failed for {dok_id}: primary error {last_err!r}; "
            f"metadata fallback also failed: {exc!r}"
        ) from exc

    content = r.text
    if "ows_EncodedAbsUrl=" not in content:
        raise RuntimeError(
            f"GO download failed for {dok_id}: no ows_EncodedAbsUrl in metadata "
            f"and primary path failed: {last_err!r}"
        )

    doc_url = content.split("ows_EncodedAbsUrl=")[1].split('"')[1]
    # GO sometimes returns the public hostname; the cert-authenticated one is
    # the "ad." prefixed variant. Force it.
    doc_url = doc_url.split("\\")[0].replace("go.aarhus", "ad.go.aarhus")

    # Fresh session for the blob fetch — matches the legacy hack, in case
    # session state interferes with SP's direct blob endpoint.
    fresh = requests.Session()
    fresh.auth = s.auth
    with fresh.get(doc_url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with open(local_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    fh.write(chunk)


# ---------------------------------------------------------------------------
# PDF conversion
# ---------------------------------------------------------------------------


def pdf_convert(
    *,
    username: str,
    password: str,
    base_url: str,
    dok_id: str,
    version_ui: str,
    timeout: int | None = None,
) -> bytes | None:
    """Convert a GO document to PDF using GO's built-in converter.

    Returns the PDF bytes on success, or ``None`` if GO declines (e.g. the
    file format isn't supported by GO's converter). Callers should fall back
    to LibreOffice / other tools when this returns ``None``.

    Uses a fresh NTLM auth (matching legacy ``GOPDFConvert``) rather than the
    shared session — GO's converter endpoint has historically been picky
    about session state.
    """
    url = f"{base_url}/_goapi/Documents/ConvertToPDF/{dok_id}/{version_ui}"
    try:
        r = requests.get(
            url,
            auth=HttpNtlmAuth(username, password),
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException:
        return None

    if r.status_code != 200:
        return None

    # GO sometimes returns 200 with an error message in the body for
    # unconvertible files.
    try:
        text_head = r.content[:512].decode("utf-8", errors="ignore")
    except Exception:  # pylint: disable=broad-except
        text_head = ""
    if "Document could not be converted" in text_head:
        return None

    return r.content
