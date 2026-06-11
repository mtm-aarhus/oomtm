"""PDF conversion helpers for MTM OO processes.

Strategy (OCR + redaction live elsewhere — added with the screening / Klargør
processes):

* already PDF            → passthrough
* office / text / html   → LibreOffice headless
* images                 → Pillow (wrapped into a single-page PDF)
* .msg / .eml            → parsed to HTML, then LibreOffice
* video / audio / unknown→ skipped (caller marks "kan ikke konverteres")

LibreOffice must be installed on the worker. Point at it with the
``LIBREOFFICE_PATH`` env var, or pass ``soffice_path`` explicitly. Default
Windows location: ``C:\\Program Files\\LibreOffice\\program\\soffice.exe``.

Heavy third-party imports (Pillow, extract_msg) are done lazily so that
processes which only need office conversion don't pay for them, and so a
missing optional dep only breaks the path that actually needs it.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Extension classification
# ---------------------------------------------------------------------------

PDF_EXTS = {"pdf"}

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "ico"}

EMAIL_EXTS = {"msg", "eml"}

# Formats LibreOffice handles well.
OFFICE_EXTS = {
    "doc", "docx", "docm", "dot", "dotx", "odt", "fodt", "rtf", "txt",
    "csv", "tsv",
    "xls", "xlsx", "xlsm", "xlsb", "xltx", "ods", "fods",
    "ppt", "pptx", "pps", "ppsx", "pot", "potx", "odp", "fodp",
    "htm", "html", "xml", "vsd", "vsdx", "pub",
}

# Things we won't try to convert. Caller marks these "kan ikke konverteres".
SKIP_EXTS = {
    # video
    "mp4", "mov", "avi", "mkv", "wmv", "flv", "webm", "m4v", "mpg", "mpeg",
    # audio
    "mp3", "wav", "m4a", "aac", "flac", "ogg", "wma",
    # archives / binaries
    "zip", "rar", "7z", "tar", "gz", "exe", "dll", "iso", "bin",
}


def classify(ext: str) -> str:
    """Return one of: 'pdf', 'image', 'email', 'office', 'skip', 'unknown'."""
    e = (ext or "").lower().lstrip(".")
    if e in PDF_EXTS:
        return "pdf"
    if e in IMAGE_EXTS:
        return "image"
    if e in EMAIL_EXTS:
        return "email"
    if e in OFFICE_EXTS:
        return "office"
    if e in SKIP_EXTS:
        return "skip"
    return "unknown"


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.digest()


# ---------------------------------------------------------------------------
# LibreOffice
# ---------------------------------------------------------------------------

_DEFAULT_SOFFICE_PATHS = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/opt/libreoffice/program/soffice",
]


def find_soffice(soffice_path: str | None = None) -> str:
    """Locate the LibreOffice binary. Order: explicit arg, LIBREOFFICE_PATH env,
    PATH lookup, then the usual install locations. Raises if not found."""
    candidates = []
    if soffice_path:
        candidates.append(soffice_path)
    env = os.getenv("LIBREOFFICE_PATH")
    if env:
        candidates.append(env)
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.extend(_DEFAULT_SOFFICE_PATHS)
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise RuntimeError(
        "LibreOffice (soffice) not found. Install it or set LIBREOFFICE_PATH."
    )


_INSTALL_LOCK = Path(tempfile.gettempdir()) / "oomtm_libreoffice_install.lock"


def _run_installer(cmd: list[str], log, timeout: int) -> None:
    log(f"LibreOffice: running {' '.join(cmd[:3])}…")
    subprocess.run(
        cmd, check=True, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _install_libreoffice(log, timeout: int) -> None:
    """Install LibreOffice with whatever package manager the machine has.

    Windows: winget → Chocolatey. Linux: apt-get → dnf. Raises with a clear
    message if no supported installer is available (the caller surfaces that
    as the document's file_note so an admin knows to install it manually).
    """
    if sys.platform.startswith("win"):
        winget = shutil.which("winget")
        if winget:
            _run_installer(
                [winget, "install", "--id", "TheDocumentFoundation.LibreOffice", "-e",
                 "--silent", "--accept-package-agreements", "--accept-source-agreements"],
                log, timeout,
            )
            return
        choco = shutil.which("choco")
        if choco:
            _run_installer([choco, "install", "libreoffice-fresh", "-y", "--no-progress"], log, timeout)
            return
        raise RuntimeError(
            "Kan ikke auto-installere LibreOffice: hverken winget eller Chocolatey "
            "findes på maskinen. Installér LibreOffice manuelt, eller sæt LIBREOFFICE_PATH."
        )
    # POSIX
    for mgr, args in (("apt-get", ["-y", "install", "libreoffice"]),
                      ("dnf", ["-y", "install", "libreoffice"])):
        exe = shutil.which(mgr)
        if exe:
            _run_installer([exe, *args], log, timeout)
            return
    raise RuntimeError("Kan ikke auto-installere LibreOffice: ingen kendt pakkemanager.")


def ensure_libreoffice(
    soffice_path: str | None = None,
    *,
    install: bool = True,
    log=None,
    install_timeout: int = 1800,
    wait_timeout: int = 1800,
) -> str:
    """Return a usable soffice path, installing LibreOffice first if missing.

    Safe to call from many parallel workers: installation is guarded by a
    lock file so only one worker installs while the others wait and then pick
    up the freshly-installed binary. A no-op (just ``find_soffice``) once
    LibreOffice is present, so it's cheap to call on every job.

    Raises ``RuntimeError`` if LibreOffice is absent and can't be installed.
    """
    log = log or (lambda *_: None)
    try:
        return find_soffice(soffice_path)
    except RuntimeError:
        if not install:
            raise

    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        # Re-check first — another worker may have just finished installing.
        try:
            return find_soffice(soffice_path)
        except RuntimeError:
            pass
        try:
            fd = os.open(str(_INSTALL_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            # Another worker holds the lock. Steal it if it's gone stale.
            try:
                if time.time() - _INSTALL_LOCK.stat().st_mtime > install_timeout:
                    _INSTALL_LOCK.unlink(missing_ok=True)
            except OSError:
                pass
            time.sleep(5)
            continue

        # We hold the lock — do the install.
        try:
            log("LibreOffice ikke fundet — installerer…")
            try:
                _install_libreoffice(log, install_timeout)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"LibreOffice-installation fejlede (exit {exc.returncode}). "
                    "Installér LibreOffice manuelt, eller sæt LIBREOFFICE_PATH."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("LibreOffice-installation timede ud.") from exc
            path = find_soffice(soffice_path)
            log(f"LibreOffice installeret: {path}")
            return path
        finally:
            _INSTALL_LOCK.unlink(missing_ok=True)

    # Timed out waiting for someone else's install — last try.
    return find_soffice(soffice_path)


def office_to_pdf(
    src: str | Path,
    out_dir: str | Path,
    *,
    soffice_path: str | None = None,
    timeout: int = 240,
) -> Path | None:
    """Convert an office/text/html file to PDF via LibreOffice headless.

    Returns the path to the produced PDF, or None if LibreOffice produced no
    output. Uses a throwaway user-profile dir per call so multiple conversions
    can run in parallel without clobbering each other's profile lock.
    """
    soffice = find_soffice(soffice_path)
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    profile = Path(tempfile.gettempdir()) / f"lo_profile_{uuid.uuid4().hex}"
    try:
        cmd = [
            soffice,
            "--headless", "--norestore", "--nolockcheck", "--nodefault",
            f"-env:UserInstallation=file:///{profile.as_posix()}",
            "--convert-to", "pdf",
            "--outdir", str(out_dir),
            str(src),
        ]
        subprocess.run(
            cmd, check=True, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    produced = out_dir / (src.stem + ".pdf")
    return produced if produced.exists() and produced.stat().st_size > 0 else None


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def image_to_pdf(src: str | Path, out_path: str | Path) -> Path | None:
    """Wrap a raster image into a single-page PDF using Pillow."""
    try:
        from PIL import Image  # lazy
    except ImportError:
        return None
    try:
        from PIL import ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True
    except Exception:  # pylint: disable=broad-except
        pass

    out_path = Path(out_path)
    try:
        with Image.open(src) as im:
            # PDF can't store alpha; flatten onto white.
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                from PIL import Image as _Image
                bg = _Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1] if im.mode == "RGBA" else None)
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.save(out_path, "PDF", resolution=150.0)
    except Exception:  # pylint: disable=broad-except
        return None
    return out_path if out_path.exists() and out_path.stat().st_size > 0 else None


# ---------------------------------------------------------------------------
# Email (.msg / .eml) → HTML → PDF
# ---------------------------------------------------------------------------


def _email_to_html(src: str | Path, ext: str) -> str | None:
    """Render an email's headers + body to a standalone HTML string.

    Attachments are NOT extracted (out of scope) — they're listed by name in a
    footer so the reviewer knows they existed.
    """
    ext = (ext or "").lower().lstrip(".")
    headers = {}
    body_html = None
    body_text = None
    attachments: list[str] = []

    if ext == "msg":
        try:
            import extract_msg  # lazy
        except ImportError:
            return None
        try:
            msg = extract_msg.openMsg(str(src))
            try:
                headers = {
                    "Fra": msg.sender or "",
                    "Til": msg.to or "",
                    "Cc": msg.cc or "",
                    "Dato": str(msg.date or ""),
                    "Emne": msg.subject or "",
                }
                body_html = getattr(msg, "htmlBody", None)
                if isinstance(body_html, bytes):
                    body_html = body_html.decode("utf-8", errors="replace")
                body_text = msg.body
                for att in (msg.attachments or []):
                    name = (att.longFilename or att.shortFilename or "ukendt").replace("\x00", "").strip()
                    if name:
                        attachments.append(name)
            finally:
                msg.close()
        except Exception:  # pylint: disable=broad-except
            return None

    elif ext == "eml":
        from email import policy
        from email.parser import BytesParser
        try:
            with open(src, "rb") as fh:
                m = BytesParser(policy=policy.default).parse(fh)
            headers = {
                "Fra": m.get("From", ""),
                "Til": m.get("To", ""),
                "Cc": m.get("Cc", ""),
                "Dato": m.get("Date", ""),
                "Emne": m.get("Subject", ""),
            }
            html_part = m.get_body(preferencelist=("html",))
            text_part = m.get_body(preferencelist=("plain",))
            if html_part is not None:
                body_html = html_part.get_content()
            if text_part is not None:
                body_text = text_part.get_content()
            for part in m.iter_attachments():
                fn = part.get_filename()
                if fn:
                    attachments.append(fn)
        except Exception:  # pylint: disable=broad-except
            return None
    else:
        return None

    def esc(s: str) -> str:
        return (str(s or "")
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    header_rows = "".join(
        f"<tr><td style='font-weight:bold;padding-right:10px;vertical-align:top'>{esc(k)}</td>"
        f"<td>{esc(v)}</td></tr>"
        for k, v in headers.items() if v
    )
    if body_html:
        body_block = body_html  # already HTML
    else:
        body_block = "<pre style='white-space:pre-wrap;font-family:inherit'>" + esc(body_text or "") + "</pre>"
    att_block = ""
    if attachments:
        items = "".join(f"<li>{esc(a)}</li>" for a in attachments)
        att_block = (
            "<hr><p style='font-weight:bold'>Vedhæftede filer "
            "(ikke medtaget i denne PDF):</p><ul>" + items + "</ul>"
        )

    return f"""<!DOCTYPE html>
<html lang="da"><head><meta charset="utf-8">
<style>body{{font-family:Arial,Helvetica,sans-serif;font-size:11pt;color:#000}}
table{{margin-bottom:14px;border-collapse:collapse}}</style></head>
<body>
<table>{header_rows}</table>
<hr>
{body_block}
{att_block}
</body></html>"""


def email_to_pdf(
    src: str | Path,
    ext: str,
    out_dir: str | Path,
    *,
    soffice_path: str | None = None,
    timeout: int = 240,
) -> Path | None:
    html = _email_to_html(src, ext)
    if html is None:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{Path(src).stem}.html"
    html_path.write_text(html, encoding="utf-8")
    try:
        return office_to_pdf(html_path, out_dir, soffice_path=soffice_path, timeout=timeout)
    finally:
        html_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def convert_to_pdf(
    src: str | Path,
    ext: str,
    out_dir: str | Path,
    *,
    soffice_path: str | None = None,
    auto_install: bool = False,
    log=None,
) -> tuple[Path | None, str, str]:
    """Convert ``src`` to PDF, choosing the method from ``ext``.

    Returns ``(pdf_path, status, note)`` where status is one of:
      * ``"ready"``   — pdf_path points to a usable PDF
      * ``"skipped"`` — deliberately not converted (video/audio/unknown)
      * ``"error"``   — conversion was attempted but failed

    For already-PDF input the original path is returned unchanged with status
    ``"ready"``.

    When ``auto_install`` is True, the LibreOffice-backed paths
    (office/email/unknown) ensure LibreOffice is installed first via
    ``ensure_libreoffice`` — installing it if the worker doesn't have it yet.
    """
    src = Path(src)
    out_dir = Path(out_dir)
    kind = classify(ext)

    if kind == "pdf":
        return src, "ready", ""

    if kind == "image":
        out = out_dir / f"{src.stem}.pdf"
        result = image_to_pdf(src, out)
        if result:
            return result, "ready", ""
        return None, "error", f"Billedet kunne ikke konverteres ({ext})."

    # The remaining kinds (office/email/unknown) need LibreOffice.
    if kind == "skip":
        return None, "skipped", f"Filtypen {ext} kan ikke konverteres til PDF (gennemse manuelt)."

    try:
        soffice_path = ensure_libreoffice(soffice_path, install=auto_install, log=log)
    except RuntimeError as exc:
        return None, "error", str(exc)

    if kind == "office":
        result = office_to_pdf(src, out_dir, soffice_path=soffice_path)
        if result:
            return result, "ready", ""
        return None, "error", f"LibreOffice kunne ikke konvertere filen ({ext})."

    if kind == "email":
        result = email_to_pdf(src, ext, out_dir, soffice_path=soffice_path)
        if result:
            return result, "ready", ""
        return None, "error", f"E-mailen kunne ikke konverteres ({ext})."

    # unknown — try LibreOffice as a last resort; it handles many odd formats.
    result = office_to_pdf(src, out_dir, soffice_path=soffice_path)
    if result:
        return result, "ready", ""
    return None, "skipped", f"Ukendt filtype ({ext}) — kunne ikke konverteres automatisk."
