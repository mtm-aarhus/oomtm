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

import io
import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
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


def create_case(
    s: requests.Session,
    *,
    base_url: str,
    metadata_xml: str,
    case_type_prefix: str = "AKT",
    web: str = "/aktindsigt",
    timeout: int = 120,
) -> dict:
    """Create a case in GO and return the parsed response, which includes:

    * ``CaseID``          — e.g. ``"AKT-2026-000782"``
    * ``CaseRelativeUrl`` — the case's web path, e.g.
      ``"cases/AKT-2026/AKT-2026-000782"`` — pass it to :func:`set_case_owner`
      (it's the ``{aktnr}/{aktid}`` ModernConfiguration path).

    ``metadata_xml`` is the ``<z:row …/>`` payload with the ows_ attributes for
    the new case (Title, Sagsprofil, Facet, Modtaget, …). ``web`` is the case web
    the AKT cases live under (the caller's proven endpoint is
    ``…/aktindsigt/_goapi/Cases``). On a non-JSON response, only ``CaseID`` is
    returned (from the raw body)."""
    url = f"{base_url}{web}/_goapi/Cases"
    payload = json.dumps({"CaseTypePrefix": case_type_prefix, "MetadataXml": metadata_xml})
    r = s.post(url, data=payload, timeout=timeout)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        return {"CaseID": r.text.strip().strip('"'), "CaseRelativeUrl": None}
    if isinstance(data, dict):
        return data
    return {"CaseID": str(data), "CaseRelativeUrl": None}


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


# Above this size, AddToCase's JSON byte-array balloons (~4×) and GO struggles —
# switch to a chunked SharePoint upload against the case's document library.
GO_SINGLE_UPLOAD_THRESHOLD = 10 * 1024 * 1024
GO_CHUNK_SIZE = 1024 * 10240  # 10 MB chunks (matches the legacy Journaliser)


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
    single_upload_threshold: int = GO_SINGLE_UPLOAD_THRESHOLD,
    created_folders: set | None = None,
    timeout: int = 1200,
) -> int | None:
    """Upload a document to a GO case and return its DocId.

    Files up to ``single_upload_threshold`` go via ``/_goapi/Documents/AddToCase``
    (``Bytes`` as a JSON array), which creates ``folder_path`` on the case itself
    if it doesn't exist yet. Larger files — or files ``AddToCase`` rejects — are
    streamed in chunks straight to the case's SharePoint document library
    (startUpload/continueUpload/finishUpload), then located and given their
    metadata. That chunked path uploads to SharePoint directly, which won't
    create the target sub-folder, so it makes a placeholder folder first; pass a
    shared ``created_folders`` set to skip that check for a ``folder_path``
    already created earlier in the run.
    """
    if len(file_bytes) <= single_upload_threshold:
        try:
            return _add_to_case(
                s, base_url=base_url, web=web, case_id=case_id, file_bytes=file_bytes,
                file_name=file_name, metadata_xml=metadata_xml, list_name=list_name,
                folder_path=folder_path, overwrite=overwrite, timeout=timeout,
            )
        except requests.RequestException:
            pass  # AddToCase failed — fall back to the chunked SharePoint upload
    return _upload_document_chunked(
        s, base_url=base_url, case_id=case_id, file_bytes=file_bytes,
        file_name=file_name, metadata_xml=metadata_xml, folder_path=folder_path,
        created_folders=created_folders, timeout=timeout,
    )


def _add_to_case(s, *, base_url, web, case_id, file_bytes, file_name, metadata_xml,
                 list_name, folder_path, overwrite, timeout):
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


# --- large-file path: chunked SharePoint upload to the GO case library -------


def case_sharepoint_url(s: requests.Session, *, base_url: str, case_id: str, timeout: int = 60) -> str | None:
    """Return a case's SharePoint web path (``ows_CaseUrl``, e.g. ``cases/AKT/…``)."""
    r = s.get(f"{base_url}/_goapi/Cases/Metadata/{case_id}/False", timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.json()["Metadata"])
    return root.attrib.get("ows_CaseUrl")


def _context_digest(s, base_url, web_path, timeout=60):
    r = s.post(f"{base_url}/{web_path}/_api/contextinfo",
               headers={"Accept": "application/json; odata=verbose"}, timeout=timeout)
    r.raise_for_status()
    return r.json()["d"]["GetContextWebInformation"]["FormDigestValue"]


def _upload_document_chunked(s, *, base_url, case_id, file_bytes, file_name,
                             metadata_xml, folder_path, created_folders=None, timeout):
    cu = case_sharepoint_url(s, base_url=base_url, case_id=case_id, timeout=timeout)
    if not cu:
        raise RuntimeError(f"Could not resolve ows_CaseUrl for GO case {case_id}")
    digest = _context_digest(s, base_url, cu, timeout)
    safe_file = file_name.replace("'", "''")
    safe_folder = (folder_path or "").replace("'", "''")
    _chunked_file_upload(s, base_url, cu, file_bytes, safe_file, digest, safe_folder,
                         created_folders, timeout)
    # GO needs a moment to index the new file before RenderListDataAsStream finds it.
    doc_id = None
    for _ in range(6):
        time.sleep(5)
        doc_id = _find_docid(s, base_url, cu, file_name, folder_path, timeout)
        if doc_id is not None:
            break
    if doc_id is not None and metadata_xml:
        try:
            set_document_metadata(s, base_url=base_url, doc_id=doc_id, metadata_xml=metadata_xml)
        except requests.RequestException:
            pass  # the file is uploaded; metadata is best-effort
    return doc_id


def _go_folder_exists(s, web_url, server_relative, timeout):
    """True iff a folder exists at ``server_relative`` (HTTP 200); any other
    response — 404, error — is treated as 'not there, try to create it'."""
    try:
        r = s.get(f"{web_url}/_api/web/GetFolderByServerRelativePath(DecodedUrl=@p)?@p='{server_relative}'",
                  headers={"Accept": "application/json; odata=verbose"}, timeout=timeout)
    except requests.RequestException:
        return False
    return r.status_code == 200


def _ensure_case_folder(s, web_url, cu, folder_path, digest, timeout):
    """Create ``folder_path`` under the case's ``Dokumenter`` library if missing,
    one segment at a time (``AddUsingPath`` needs the parent to exist). Idempotent
    — an already-existing folder is fine. Only the chunked upload path needs this;
    ``AddToCase`` creates folders itself. ``folder_path`` is already apostrophe-
    escaped for the OData literal by the caller."""
    write_h = {"X-RequestDigest": digest, "X-FORMS_BASED_AUTH_ACCEPTED": "f",
               "Accept": "application/json; odata=verbose"}
    parent = f"/{cu}/Dokumenter"
    for seg in [p for p in folder_path.replace("\\", "/").split("/") if p]:
        target = f"{parent}/{seg}"
        url = f"{web_url}/_api/web/folders/AddUsingPath(DecodedUrl=@p)?@p='{target}'"
        r = s.post(url, headers=write_h, timeout=timeout)
        # 200/201 = created. Otherwise it may already exist (created by an earlier
        # run) — accept that; only raise if it's genuinely not there.
        if r.status_code not in (200, 201) and not _go_folder_exists(s, web_url, target, timeout):
            r.raise_for_status()
        parent = target


def _chunked_file_upload(s, base_url, cu, binary, file_name, digest, folder_path,
                         created_folders, timeout):
    web_url = f"{base_url}/{cu}"
    # Per-request headers (don't pollute the shared session with a stale digest).
    write_h = {"X-RequestDigest": digest, "X-FORMS_BASED_AUTH_ACCEPTED": "f"}
    chunk_h = {**write_h, "Content-Type": "application/octet-stream"}
    target_folder = (f"/{cu}/Dokumenter/{folder_path}".replace("\\", "/")
                     if folder_path else f"/{cu}/Dokumenter")
    # SharePoint won't create the sub-folder on Files/add, so make a placeholder
    # first — once per folder path per run (the AddToCase path doesn't need this).
    if folder_path and (created_folders is None or folder_path not in created_folders):
        _ensure_case_folder(s, web_url, cu, folder_path, digest, timeout)
        if created_folders is not None:
            created_folders.add(folder_path)
    create_url = (f"{web_url}/_api/web/GetFolderByServerRelativePath(DecodedUrl=@p)/Files/"
                  f"add(url=@f,overwrite=true)?@p='{target_folder}'&@f='{file_name}'")
    s.post(create_url, headers=write_h, timeout=timeout).raise_for_status()
    target = f"{target_folder}%2F{file_name}"
    uid = str(uuid.uuid4())
    offset, total = 0, len(binary)
    base = f"{web_url}/_api/web/GetFileByServerRelativePath(DecodedUrl=@u)"
    with io.BytesIO(binary) as stream:
        first = True
        while True:
            buf = stream.read(GO_CHUNK_SIZE)
            if not buf:
                break
            if first and len(buf) == total:
                s.post(f"{base}/startUpload(uploadId=guid'{uid}')?@u='{target}'", data=buf, headers=chunk_h, timeout=timeout).raise_for_status()
                s.post(f"{base}/finishUpload(uploadId=guid'{uid}',fileOffset={offset})?@u='{target}'", data=buf, headers=chunk_h, timeout=timeout).raise_for_status()
                break
            if first:
                s.post(f"{base}/startUpload(uploadId=guid'{uid}')?@u='{target}'", data=buf, headers=chunk_h, timeout=timeout).raise_for_status()
                first = False
            elif stream.tell() == total:
                s.post(f"{base}/finishUpload(uploadId=guid'{uid}',fileOffset={offset})?@u='{target}'", data=buf, headers=chunk_h, timeout=timeout).raise_for_status()
            else:
                s.post(f"{base}/continueUpload(uploadId=guid'{uid}',fileOffset={offset})?@u='{target}'", data=buf, headers=chunk_h, timeout=timeout).raise_for_status()
            offset += len(buf)


def _find_docid(s, base_url, cu, file_name, folder_path, timeout):
    """Locate the DocID of a just-uploaded file by FileLeafRef (paged scan)."""
    r = s.get(f"{base_url}/{cu}/_goapi/Administration/GetLeftMenuCounter", timeout=timeout)
    r.raise_for_status()
    view_id = next((it.get("ViewId") for it in r.json()
                    if it.get("ViewName") == "AllItems.aspx" and it.get("ListName") == "Dokumenter"), None)
    if view_id is None:
        return None
    list_url = f"'/{cu}/Dokumenter'"
    root_folder = f"/{cu}/Dokumenter" + (f"/{folder_path}" if folder_path else "")
    url = (f"{base_url}/{cu}/_api/web/GetList(@listUrl)/RenderListDataAsStream"
           f"?@listUrl={list_url}&View={view_id}&RootFolder={root_folder}")
    headers = {"content-type": "application/json;odata=verbose"}
    payload = json.dumps({"parameters": {"__metadata": {"type": "SP.RenderListDataParameters"},
                          "ViewXml": '<View><Query></Query><RowLimit Paged="TRUE">100</RowLimit></View>'}})
    target = str(file_name).lower()
    while True:
        r = s.post(url, headers=headers, data=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        for row in data.get("Row", []):
            if str(row.get("FileLeafRef")).lower() == target:
                return row.get("DocID")
        nxt = data.get("NextHref")
        if not nxt:
            return None
        url = (f"{base_url}/{cu}/_api/web/GetList(@listUrl)/RenderListDataAsStream"
               f"?@listUrl={list_url}{nxt.replace('?', '&', 1)}")


_RE_OWS_DATO = re.compile(r'ows_Dato="(\d{2})-(\d{2})-(\d{4})"')


def set_document_metadata(s: requests.Session, *, base_url: str, doc_id, metadata_xml: str,
                          web: str = "", timeout: int = 600) -> requests.Response:
    """Set a GO document's ows_ metadata. GO's Documents/Metadata expects
    ``ows_Dato`` as MM-DD-YYYY, so a DD-MM-YYYY value is flipped before sending."""
    metadata_xml = _RE_OWS_DATO.sub(
        lambda m: f'ows_Dato="{m.group(2)}-{m.group(1)}-{m.group(3)}"', metadata_xml)
    r = s.post(f"{base_url}{web}/_goapi/Documents/Metadata",
               data={"DocId": doc_id, "MetadataXml": metadata_xml}, timeout=timeout)
    r.raise_for_status()
    return r


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


def delete_document(
    s: requests.Session,
    *,
    base_url: str,
    doc_id,
    force_delete: bool = True,
    web: str = "",
    timeout: int = 120,
) -> requests.Response:
    """Delete a document from GO by DocId (``DELETE /_goapi/Documents/ByDocumentId``).

    ``force_delete`` removes it even if it's checked out / finalised / a case
    record — needed because we mark journalised docs as case records. A document
    that's already gone counts as success (idempotent)."""
    url = f"{base_url}{web}/_goapi/Documents/ByDocumentId"
    r = s.delete(url, data=json.dumps({"DocId": doc_id, "ForceDelete": force_delete}), timeout=timeout)
    if r.status_code in (404,) or (r.status_code == 200 and "does not exist" in (r.text or "").lower()):
        return r
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# Case owner — set the GO CaseOwner from an e-mail (the PeoplePicker "hack").
#
# A People field can't be set through MetadataXml, so we mimic what the UI does:
#   1. fetch a FormDigest for the root web (PeoplePicker) and for the case web,
#   2. PeoplePicker-search the user by e-mail,
#   3. ValidateUpdateListItem the case's list item with the CaseOwner field.
# Ported from the caseworker's proven script. The ModernConfiguration lookup
# uses the case's web path (CaseRelativeUrl from create_case, == ows_CaseUrl) —
# in the legacy code that was /cases/{aktnr}/{aktid} where
# aktnr = CaseRelativeUrl.split('/')[-2] and aktid = CaseID, i.e. the same path.
# set_case_owner is best-effort (callers should not fail the whole journalisation
# if it raises).
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


def _modern_list_and_item(s, base_url, case_relative_url, timeout=60):
    """Return (list name, list-item id) for a case from its ModernConfiguration.
    ``case_relative_url`` is the case web path (CaseRelativeUrl / ows_CaseUrl),
    e.g. ``cases/AKT-2026/AKT-2026-000782``."""
    endpoint = f"{base_url}/{case_relative_url}/_goapi/Administration/ModernConfiguration"
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
    raise RuntimeError(f"GO ModernConfiguration empty after 5 tries for {case_relative_url}: {last}")


def set_case_owner(
    s: requests.Session,
    *,
    base_url: str,
    case_id: str,
    caseworker_email: str,
    case_relative_url: str | None = None,
    case_web: str = "/aktindsigt",
    timeout: int = 60,
) -> bool:
    """Set the GO CaseOwner of ``case_id`` to the user with ``caseworker_email``.

    ``case_relative_url`` is the case web path (the create response's
    ``CaseRelativeUrl``, e.g. ``cases/AKT-2026/AKT-2026-000782``); if omitted it's
    resolved from ``ows_CaseUrl``. The ModernConfiguration lookup runs against
    that path, the CaseOwner update against ``case_web`` (the /aktindsigt list).

    Returns True on success, False if no matching user was found. Raises on HTTP
    errors — callers should treat this as best-effort and not fail the whole
    journalisation if it raises (the case still exists; the owner can be set in
    GO manually)."""
    if not case_relative_url:
        case_relative_url = case_sharepoint_url(s, base_url=base_url, case_id=case_id, timeout=timeout)
    if not case_relative_url:
        raise RuntimeError(f"Could not resolve the case web path for {case_id}")
    root_digest = _form_digest(s, base_url, timeout)
    case_digest = _form_digest(s, f"{base_url}{case_web}", timeout)
    listnumber, item_id = _modern_list_and_item(s, base_url, case_relative_url, timeout)
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
