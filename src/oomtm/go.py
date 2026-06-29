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
from urllib.parse import quote

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


# ---------------------------------------------------------------------------
# Cases — create / update metadata / close  (write operations)
# ---------------------------------------------------------------------------


def _extract_case_id(r: requests.Response) -> str:
    """Pull the new CaseID out of a Create-Case response (tolerates shapes)."""
    try:
        data = r.json()
    except ValueError:
        return r.text.strip().strip('"')
    if isinstance(data, dict):
        return str(data.get("CaseID") or data.get("CaseId") or data.get("Id") or data)
    return str(data)


def create_case(
    s: requests.Session,
    *,
    base_url: str,
    metadata_xml: str,
    case_type_prefix: str = "AKT",
    web: str = "/aktindsigt",
    timeout: int = 120,
) -> str:
    """Create a case in GO and return its CaseID (e.g. ``"AKT-2026-000782"``).

    ``metadata_xml`` is the ``<z:row …/>`` payload with the ows_ attributes for
    the new case (Title, Sagsprofil, Facet, Modtaget, …). ``web`` is the case
    web the AKT cases live under (the caller's proven endpoint is
    ``…/aktindsigt/_goapi/Cases``).
    """
    url = f"{base_url}{web}/_goapi/Cases"
    payload = json.dumps({"CaseTypePrefix": case_type_prefix, "MetadataXml": metadata_xml})
    r = s.post(url, data=payload, timeout=timeout)
    r.raise_for_status()
    return _extract_case_id(r)


def set_case_metadata(
    s: requests.Session,
    *,
    base_url: str,
    case_id: str,
    metadata_xml: str,
    attributes: dict | None = None,
    web: str = "",
    timeout: int = 120,
) -> requests.Response:
    """Update a case's ows_ metadata via ``/_goapi/Cases/Metadata`` (routes by
    ``CaseId``, so the root web is fine). ``attributes`` is an optional
    ``{DBAttribute: value}`` map for non-ows fields."""
    url = f"{base_url}{web}/_goapi/Cases/Metadata"
    body: dict = {"CaseId": case_id, "MetadataXml": metadata_xml}
    if attributes:
        body["Attributes"] = attributes
    r = s.post(url, data=json.dumps(body), timeout=timeout)
    r.raise_for_status()
    return r


def close_case(
    s: requests.Session,
    *,
    base_url: str,
    case_id: str,
    web: str = "",
    timeout: int = 120,
) -> requests.Response:
    """Close a GO case."""
    url = f"{base_url}{web}/_goapi/Cases/CloseCase"
    r = s.post(url, data=json.dumps({"CaseId": case_id}), timeout=timeout)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# Documents — upload / journalize  (write operations)
# ---------------------------------------------------------------------------


def upload_document(
    s: requests.Session,
    *,
    base_url: str,
    case_id: str,
    file_bytes: bytes,
    file_name: str,
    metadata_xml: str = '<z:row xmlns:z="#RowsetSchema"/>',
    list_name: str = "Dokumenter",
    folder_path: str = "",
    overwrite: bool = True,
    web: str = "",
    timeout: int = 1200,
) -> int | None:
    """Upload a document to a GO case via ``/_goapi/Documents/AddToCase`` and
    return the new DocId.

    ``Bytes`` is sent as a JSON array of byte values (GO's documented shape). GO
    silently struggles with very large files this way — the legacy Journaliser
    falls back to a SharePoint chunked upload above ~10 MB; that fallback isn't
    ported here, so keep individual uploads modest (delivered redacted PDFs and
    rendered e-mails are well within range).
    """
    url = f"{base_url}{web}/_goapi/Documents/AddToCase"
    payload = {
        "Bytes": list(file_bytes),
        "CaseId": case_id,
        "ListName": list_name,
        "FolderPath": folder_path,
        "FileName": file_name,
        "Metadata": metadata_xml,
        "Overwrite": overwrite,
    }
    r = s.post(url, data=json.dumps(payload), timeout=timeout)
    r.raise_for_status()
    try:
        return r.json().get("DocId")
    except ValueError:
        return None


def mark_as_case_record(
    s: requests.Session,
    *,
    base_url: str,
    doc_ids: list,
    web: str = "",
    timeout: int = 300,
) -> requests.Response:
    """Journalize (mark as case record) the given documents — required before a
    case can be considered finalised."""
    url = f"{base_url}{web}/_goapi/Documents/MarkMultipleAsCaseRecord/ByDocumentId"
    r = s.post(url, data=json.dumps({"DocumentIds": list(doc_ids)}), timeout=timeout)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# Case owner — set the GO CaseOwner from an e-mail (the PeoplePicker "hack").
#
# A People field can't be set through MetadataXml, so we mimic what the UI does:
#   1. fetch a FormDigest for the root web (PeoplePicker) and for the case web,
#   2. PeoplePicker-search the user by e-mail,
#   3. ValidateUpdateListItem the case's list item with the CaseOwner field.
# Ported from the caseworker's proven script. The ``case_group`` path segment in
# the ModernConfiguration URL is tenant-specific — pass the value GO uses for
# the case's folder; set_case_owner is best-effort (callers should not fail the
# whole journalisation if it raises).
# ---------------------------------------------------------------------------


def _form_digest(s: requests.Session, site_url: str, timeout: int = 60) -> str:
    r = s.post(f"{site_url}/_api/contextinfo",
               headers={"Accept": "application/json; odata=verbose"}, timeout=timeout)
    r.raise_for_status()
    return r.json()["d"]["GetContextWebInformation"]["FormDigestValue"]


def search_user(s: requests.Session, root_url: str, digest: str, email: str, timeout: int = 60):
    """PeoplePicker-search and return the entity whose Email matches, else None."""
    endpoint = (f"{root_url}/_api/SP.UI.ApplicationPages.ClientPeoplePicker"
                f"WebServiceInterface.ClientPeoplePickerSearchUser")
    headers = {
        "Accept": "application/json;odata=verbose",
        "Content-Type": "application/json;odata=verbose",
        "X-RequestDigest": digest,
    }
    payload = {"queryParams": {
        "QueryString": email, "MaximumEntitySuggestions": 50,
        "AllowEmailAddresses": False, "AllowOnlyEmailAddresses": False,
        "PrincipalType": 1, "PrincipalSource": 15, "SharePointGroupID": 0,
    }}
    r = s.post(endpoint, headers=headers, data=json.dumps(payload), timeout=timeout)
    r.raise_for_status()
    results = json.loads(r.json()["d"]["ClientPeoplePickerSearchUser"])
    for entity in results:
        em = entity.get("EntityData", {}).get("Email")
        if em and em.lower() == email.lower():
            return entity
    return None


def _modern_list_and_item(s, base_url, case_id, case_group, timeout=60):
    endpoint = f"{base_url}/cases/{case_group}/{case_id}/_goapi/Administration/ModernConfiguration"
    payload = {"providerTypes": ["ModernCase", "MoveDocument", "Insight", "SearchSystem", "UserSettings"]}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    last = ""
    for _ in range(5):
        r = s.post(endpoint, headers=headers, json=payload, timeout=timeout)
        if r.status_code == 200 and r.text.strip():
            mc = r.json().get("ModernCase") or {}
            caselist = (mc.get("ItemServerUrl") or "").split("/")[-2]
            return caselist, mc.get("ListItemID")
        last = (r.text or "")[:200]
        time.sleep(5)
    raise RuntimeError(f"GO ModernConfiguration empty after 5 tries for {case_id}: {last}")


def set_case_owner(
    s: requests.Session,
    *,
    base_url: str,
    case_id: str,
    case_group: str,
    caseworker_email: str,
    case_web: str = "/aktindsigt",
    timeout: int = 60,
) -> bool:
    """Set the GO CaseOwner of ``case_id`` to the user with ``caseworker_email``.

    Returns True on success, False if no matching user was found. Raises on HTTP
    errors — callers should treat this as best-effort and not fail the whole
    journalisation if it raises (the case still exists; the owner can be set in
    GO manually)."""
    root_digest = _form_digest(s, base_url, timeout)
    case_digest = _form_digest(s, f"{base_url}{case_web}", timeout)
    listnumber, item_id = _modern_list_and_item(s, base_url, case_id, case_group, timeout)
    entity = search_user(s, base_url, root_digest, caseworker_email, timeout)
    if not entity:
        return False
    list_url = quote(f"{case_web}/Lists/{listnumber}", safe="")
    endpoint = (f"{base_url}{case_web}/_api/web/GetList(@a1)/items(@a2)/ValidateUpdateListItem()"
                f"?@a1='{list_url}'&@a2='{item_id}'")
    headers = {
        "Accept": "application/json;odata=verbose",
        "Content-Type": "application/json;odata=verbose",
        "X-RequestDigest": case_digest,
        "X-Sp-Requestresources": f"listUrl={list_url}",
    }
    body = {
        "formValues": [{"FieldName": "CaseOwner", "FieldValue": json.dumps([entity]),
                        "HasException": False, "ErrorMessage": None}],
        "bNewDocumentUpdate": False, "checkInComment": None,
    }
    r = s.post(endpoint, headers=headers, data=json.dumps(body), timeout=timeout)
    r.raise_for_status()
    return True
