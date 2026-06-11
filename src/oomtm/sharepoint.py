"""SharePoint Online helpers for MTM OO processes.

Wraps the patterns we use across robots:

* Cert-based authentication via Office365-REST-Python-Client
* Folder hierarchy creation (the SDK's ``folders.add`` does NOT create parents)
* Chunked upload for files near or above SharePoint's ~250 MB single-PUT limit
* Streaming download via ``download_session``
* Share-link generation
* Filename / folder-name sanitization tailored to SharePoint Online's character
  blocklist and 400-char URL limit (lifted verbatim from the legacy
  ``Python_AktBob2-GenerererAktindsigter`` robot).

Credentials are always passed in by the caller. The lib never touches
OpenOrchestrator directly.
"""
from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

if TYPE_CHECKING:
    from office365.sharepoint.client_context import ClientContext


# ---------------------------------------------------------------------------
# Filename / folder-name sanitization
# ---------------------------------------------------------------------------

# Stripped chars (verbatim from legacy sanitize_title). Anything not in
# [a-zA-Z0-9ÆØÅæøå space] after the first pass is removed by the final regex.
_BAD_PUNCTUATION = re.compile(r"[.:>#<*\?/%&{}\$!\"@+\|'=]+")
_NON_ALLOWED = re.compile(r"[^a-zA-Z0-9ÆØÅæøå ]")
_MULTI_SPACE = re.compile(r" {2,}")


def sanitize_title(title: str) -> str:
    """Sanitize a string for use in a SharePoint folder or file name.

    Removes characters SharePoint Online forbids in paths plus all punctuation
    (we found over the years that even allowed chars like '-' cause issues
    when combined with other tooling). Keeps Danish characters æøåÆØÅ.

    Lifted verbatim from the legacy ``PrepareEachDocumentToUpload.sanitize_title``
    so naming stays byte-identical with files uploaded by older robots.
    """
    if title is None:
        return ""
    s = str(title).replace('"', "")
    s = _BAD_PUNCTUATION.sub("", s)
    s = s.replace("\n", "").replace("\r", "")
    s = s.strip()
    s = _NON_ALLOWED.sub("", s)
    s = _MULTI_SPACE.sub(" ", s)
    return s


_SP_FORBIDDEN = re.compile(r'[~"#%&*:<>?/\\{}|]')


def sanitize_segment(s: str) -> str:
    """Light sanitization for a folder name or case number that must stay
    readable — removes only the characters SharePoint Online forbids in path
    segments, but KEEPS hyphens, underscores, dots and Danish letters.

    Use this for folder names (case folders, GO/Nova case numbers like
    ``GEO-2024-000170``); use ``sanitize_title`` for the document *title* part
    of a filename, where the legacy robots stripped punctuation entirely.
    """
    s = _SP_FORBIDDEN.sub("", str(s or ""))
    s = s.replace("\n", " ").replace("\r", " ").strip()
    s = s.strip(".").strip()
    s = _MULTI_SPACE.sub(" ", s)
    return s


def truncate_title(
    title: str,
    *,
    base_path: str,
    overmappe: str,
    undermappe: str,
    akt_id,
    dok_id,
    max_path_length: int = 400,
) -> str:
    """Truncate a sanitized title so the full SharePoint URL stays under the limit.

    SharePoint Online's hard URL limit is ~400 chars. ``base_path`` is the
    server-relative path up to (but not including) the case folder, e.g.::

        "Teams/tea-teamsite12593/Delte dokumenter/"

    The final filename is assumed to look like ``"{akt_id:04} - {dok_id} - {title}.ext"``;
    we reserve 7 chars for the separators and extension.

    Lifted from legacy ``calculate_available_title_length``.
    """
    fixed = (
        len(base_path)
        + len(overmappe)
        + len(undermappe)
        + len(str(akt_id))
        + len(str(dok_id))
        + 7
    )
    available = max_path_length - fixed
    if len(title) > available:
        return title[:max(0, available)]
    return title


def build_filename(akt_id: int, dok_id, title: str, ext: str) -> str:
    """Build the canonical KontAKT/AktBob document filename.

    Format: ``{akt_id:04} - {dok_id} - {title}.{ext}``

    ``ext`` should be the bare extension (``"pdf"``, not ``".pdf"``).
    """
    ext = (ext or "").lstrip(".")
    return f"{int(akt_id):04} - {dok_id} - {title}.{ext}"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect(
    *,
    site_url: str,
    tenant: str,
    client_id: str,
    thumbprint: str,
    cert_path: str,
) -> "ClientContext":
    """Return a cert-authenticated SharePoint ClientContext.

    All four cert params correspond to the values OO stores under the
    ``SharePointCert`` / ``SharePointAPI`` credentials in legacy robots.
    """
    # Imported lazily so processes that don't need SharePoint don't pay the
    # Office365 import cost.
    from office365.sharepoint.client_context import ClientContext

    return ClientContext(site_url).with_client_certificate(
        tenant=tenant,
        client_id=client_id,
        thumbprint=thumbprint,
        cert_path=cert_path,
    )


# ---------------------------------------------------------------------------
# Path construction
# ---------------------------------------------------------------------------


def build_server_relative_path(site_url: str, library: str, *parts: str) -> str:
    """Build a server-relative SharePoint path.

    Example::

        >>> build_server_relative_path(
        ...     "https://aarhuskommune.sharepoint.com/Teams/tea-teamsite12593",
        ...     "Delte dokumenter",
        ...     "42 - Some case",
        ...     "go-12345 - GO Case",
        ... )
        '/Teams/tea-teamsite12593/Delte dokumenter/42 - Some case/go-12345 - GO Case'
    """
    site_path = urlparse(site_url).path.rstrip("/")
    segments = [site_path.lstrip("/"), library.strip("/")]
    for p in parts:
        if p:
            segments.append(p.strip("/"))
    return "/" + "/".join(segments)


def site_root_path(site_url: str) -> str:
    """Return just the path portion of the site URL (e.g. '/Teams/tea-teamsite12593')."""
    return urlparse(site_url).path.rstrip("/")


# ---------------------------------------------------------------------------
# Folder ops
# ---------------------------------------------------------------------------


def folder_exists(ctx: "ClientContext", server_relative_path: str) -> bool:
    """Return True if the given folder exists on SharePoint."""
    try:
        folder = ctx.web.get_folder_by_server_relative_url(server_relative_path)
        ctx.load(folder)
        ctx.execute_query()
        return True
    except Exception:  # pylint: disable=broad-except
        return False


def ensure_folder(
    ctx: "ClientContext",
    *,
    parent_path: str,
    segments: list[str],
) -> str:
    """Ensure all segments exist as nested folders under ``parent_path``.

    ``parent_path`` (e.g. ``/Teams/.../Delte dokumenter``) is assumed to
    already exist. Each segment is created in turn if missing.

    Returns the full server-relative path of the deepest folder.
    """
    current = parent_path.rstrip("/")
    for seg in segments:
        seg = (seg or "").strip("/")
        if not seg:
            continue
        current = f"{current}/{seg}"
        if not folder_exists(ctx, current):
            ctx.web.folders.add(current).execute_query()
    return current


def delete_folder(ctx: "ClientContext", server_relative_path: str) -> None:
    """Recursively delete a folder and everything inside it."""
    folder = ctx.web.get_folder_by_server_relative_url(server_relative_path)
    folder.delete_object().execute_query()


def list_folder(ctx: "ClientContext", server_relative_path: str) -> list[str]:
    """List filenames directly inside ``server_relative_path`` (non-recursive)."""
    folder = ctx.web.get_folder_by_server_relative_url(server_relative_path)
    files = folder.files
    ctx.load(files)
    ctx.execute_query()
    return [f.properties.get("Name") for f in files]


# ---------------------------------------------------------------------------
# File ops
# ---------------------------------------------------------------------------

# SharePoint Online's documented per-PUT cap is ~250 MB. We use 249 MB as the
# safe threshold (matches legacy SharePointUploader).
DEFAULT_SINGLE_UPLOAD_THRESHOLD = 249 * 1024 * 1024
DEFAULT_CHUNK_SIZE = 1_000_000  # 1 MB chunks


def upload_file(
    ctx: "ClientContext",
    *,
    folder_path: str,
    local_file: str,
    overwrite: bool = True,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    single_upload_threshold: int = DEFAULT_SINGLE_UPLOAD_THRESHOLD,
    progress_cb=None,
) -> None:
    """Upload ``local_file`` to the given SharePoint folder.

    Uses a single PUT for files <= ``single_upload_threshold``; falls back to
    chunked upload via ``create_upload_session`` for larger files (or if the
    single-PUT path raises).
    """
    folder = ctx.web.get_folder_by_server_relative_path(folder_path)
    ctx.load(folder)
    ctx.execute_query()

    file_name = os.path.basename(local_file)
    file_size = os.path.getsize(local_file)

    if file_size <= single_upload_threshold:
        try:
            with open(local_file, "rb") as fh:
                folder.files.add(file_name, fh.read(), overwrite=overwrite).execute_query()
            if progress_cb:
                progress_cb(file_size, file_size)
            return
        except Exception:  # pylint: disable=broad-except
            # Fall through to chunked upload
            pass

    def _progress(offset: int) -> None:
        if progress_cb:
            progress_cb(offset, file_size)

    with open(local_file, "rb") as fh:
        folder.files.create_upload_session(fh, chunk_size, _progress).execute_query()


def download_file(
    ctx: "ClientContext",
    *,
    file_path: str,
    local_path: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> None:
    """Stream a file from SharePoint to local disk.

    Uses ``download_session`` so very large files don't hit memory limits.
    """
    file = ctx.web.get_file_by_server_relative_url(file_path)
    with open(local_path, "wb") as fh:
        file.download_session(fh, chunk_size).execute_query()


def delete_file(ctx: "ClientContext", file_path: str) -> None:
    """Delete a single file from SharePoint."""
    file = ctx.web.get_file_by_server_relative_url(file_path)
    file.delete_object().execute_query()


# ---------------------------------------------------------------------------
# High-level convenience
# ---------------------------------------------------------------------------


def upload_to_case_folder(
    ctx: "ClientContext",
    *,
    site_url: str,
    library: str,
    overmappe: str,
    undermappe: str,
    local_file: str,
    overwrite: bool = True,
) -> str:
    """Ensure ``library/overmappe/undermappe`` exists and upload ``local_file``
    into it. Returns the file's server-relative path.

    This is the one-call path the KontAKT ToPDF robots use: build folder path →
    ensure both folder levels exist → (chunked) upload.
    """
    parent = build_server_relative_path(site_url, library)
    folder_path = ensure_folder(ctx, parent_path=parent, segments=[overmappe, undermappe])
    upload_file(ctx, folder_path=folder_path, local_file=local_file, overwrite=overwrite)
    return f"{folder_path}/{os.path.basename(local_file)}"


def file_browser_url(site_url: str, file_server_relative_path: str) -> str:
    """Build a clickable browser URL from a server-relative file path.

    ``site_url`` e.g. ``https://aarhuskommune.sharepoint.com/Teams/tea-teamsite12593``;
    ``file_server_relative_path`` e.g. ``/Teams/tea-teamsite12593/Delte dokumenter/.../x.pdf``.
    """
    parsed = urlparse(site_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin + quote(file_server_relative_path)


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


# Common SharingLinkKind values from office365.sharepoint.sharing.links.kind
# Re-stated here so callers don't need to import the office365 lib for a constant.
SHARING_LINK_ORGANIZATION_VIEW = 2
SHARING_LINK_ORGANIZATION_EDIT = 3
SHARING_LINK_ANONYMOUS_VIEW = 4
SHARING_LINK_ANONYMOUS_EDIT = 5
SHARING_LINK_FLEXIBLE = 6


def get_share_link(
    ctx: "ClientContext",
    *,
    path: str,
    kind: int = SHARING_LINK_ORGANIZATION_VIEW,
) -> str:
    """Generate a sharing link for a folder or file. Returns the URL."""
    target = ctx.web.get_folder_by_server_relative_url(path)
    result = target.share_link(kind).execute_query()
    return result.value.sharingLinkInfo.Url
