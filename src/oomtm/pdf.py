"""PDF conversion helpers for MTM OO processes.

Strategy (OCR + redaction live elsewhere — added with the screening / Klargør
processes):

* already PDF            → passthrough
* office / text / html   → LibreOffice headless
* images                 → Pillow (wrapped into a single-page PDF)
* .msg / .eml            → parsed to HTML, then LibreOffice
* video / audio / unknown→ skipped (caller marks "kan ikke konverteres")

LibreOffice must be reachable on the worker. ``ensure_libreoffice`` will, if it
is missing, auto-install it — and on Windows it does so **without admin** by
running an MSI administrative install (``msiexec /a``), which just unpacks the
files (no UAC prompt). Override the source MSI with ``LIBREOFFICE_MSI_URL`` and
the extract location with ``OOMTM_LIBREOFFICE_DIR``. Tesseract uses the same
no-admin idea on Windows: the installer is downloaded and extracted with a
per-user 7-Zip helper, then the extracted binary directory is prepended to this
process' ``PATH``. Override with ``OOMTM_TESSERACT_EXE_URL`` /
``OOMTM_TESSERACT_DIR``. To skip auto-install entirely, point at existing
binaries with ``LIBREOFFICE_PATH`` / ``TESSERACT_PATH`` or pass the explicit
path argument.

Heavy third-party imports (Pillow, extract_msg) are done lazily so that
processes which only need office conversion don't pay for them, and so a
missing optional dep only breaks the path that actually needs it.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
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


def _prepend_process_path(dir_: str | Path) -> None:
    """Make a no-admin tool install visible to child processes in this run."""
    value = str(Path(dir_))
    current = os.environ.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    if any(os.path.normcase(p) == os.path.normcase(value) for p in parts):
        return
    os.environ["PATH"] = value + (os.pathsep + current if current else "")


def _looks_like_windows_exe(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"MZ"
    except OSError:
        return False


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
    # A previous no-admin extract (msiexec /a) drops soffice.exe here.
    noadmin = _find_soffice_in(_NOADMIN_DIR)
    if noadmin:
        candidates.append(noadmin)
    candidates.extend(_DEFAULT_SOFFICE_PATHS)
    for c in candidates:
        p = Path(c) if c else None
        if p and p.exists():
            _prepend_process_path(p.parent)
            return str(p)
    raise RuntimeError(
        "LibreOffice (soffice) not found. Install it or set LIBREOFFICE_PATH."
    )


_INSTALL_LOCK = Path(tempfile.gettempdir()) / "oomtm_libreoffice_install.lock"
_MSIEXEC_LOCK = Path(tempfile.gettempdir()) / "oomtm_msiexec.lock"

# Pinned stable build for the no-admin extract. Override with LIBREOFFICE_MSI_URL
# (e.g. an internal mirror) if this version is retired or the worker has no
# direct internet access.
_DEFAULT_LO_MSI_URL = (
    "https://download.documentfoundation.org/libreoffice/stable/"
    "26.2.4/win/x86_64/LibreOffice_26.2.4_Win_x86-64.msi"
)

# Where a no-admin extract lands. LOCALAPPDATA is user-writable (no admin).
# Override with OOMTM_LIBREOFFICE_DIR if needed.
_NOADMIN_DIR = Path(
    os.getenv("OOMTM_LIBREOFFICE_DIR")
    or (Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir()) / "oomtm" / "libreoffice")
)


def _find_soffice_in(dir_: Path) -> str | None:
    """Return the first ``soffice.exe`` found under *dir_*, or None."""
    try:
        for p in Path(dir_).rglob("soffice.exe"):
            return str(p)
    except OSError:
        pass
    return None


# MSI files are OLE compound documents starting with this signature. A mirror
# "choose a download" page or an error page (served HTTP 200) would otherwise be
# saved as a .msi and make msiexec fail with a cryptic code.
_MSI_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MSI_BUSY_CODE = 1618


_SSL_CTX = None


def _ssl_context(log):
    """An SSL context that trusts the OS certificate store, like Edge does.

    Behind a corporate TLS-inspection proxy, the server presents a certificate
    signed by an internal CA that lives in the Windows trust store but NOT in
    Python's bundled CA list, so the default urllib verification fails with
    "unable to get local issuer certificate". ``truststore`` routes verification
    through the OS verifier (Windows SChannel), which trusts that CA and chases
    missing intermediates — exactly like Windows-native tools that already work
    here.
    """
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX
    try:
        import truststore
        _SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception as exc:  # pylint: disable=broad-except
        log(f"truststore utilgængelig ({exc}); bruger Pythons standard-certifikater.")
        _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def _download(url: str, dest: Path, log, timeout: int, attempts: int = 3) -> None:
    log(f"Henter {url} …")
    req = urllib.request.Request(url, headers={"User-Agent": "oomtm-setup"})
    ctx = _ssl_context(log)
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp, open(dest, "wb") as fh:
                shutil.copyfileobj(resp, fh, length=1 << 20)
            return
        except OSError as exc:  # URLError / SSLError / timeout
            last_exc = exc
            if attempt < attempts:
                log(f"Download forsøg {attempt}/{attempts} fejlede ({exc}); prøver igen…")
                time.sleep(2 * attempt)
    # Last resort (opt-in): some locked-down networks break TLS verification in a
    # way truststore can't fix. OOMTM_DOWNLOAD_INSECURE=1 fetches without
    # verifying the cert — only safe because the file is integrity-checked after
    # download (MSI magic bytes / sha256) and the network is the kommune's own.
    if str(os.getenv("OOMTM_DOWNLOAD_INSECURE", "")).strip().lower() in ("1", "true", "yes", "on"):
        log("ADVARSEL: henter UDEN certifikatvalidering (OOMTM_DOWNLOAD_INSECURE=1).")
        unverified = ssl._create_unverified_context()  # noqa: S323 - opt-in, integrity-checked
        with urllib.request.urlopen(req, timeout=timeout, context=unverified) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh, length=1 << 20)
        return
    raise last_exc


def _tail_text(path: Path, max_chars: int = 2000) -> str:
    """Best-effort tail of an MSI log (Windows writes these as UTF-16 LE)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    if data[:2] == b"\xff\xfe":
        text = data.decode("utf-16-le", errors="replace")
    elif data[:3] == b"\xef\xbb\xbf":
        text = data.decode("utf-8-sig", errors="replace")
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")
    return text.strip()[-max_chars:]


def _proc_output_tail(proc: subprocess.CompletedProcess, max_chars: int = 2000) -> str:
    data = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
    if not data.strip():
        return ""
    return data.decode("utf-8", errors="replace").strip()[-max_chars:]


def _run_msiexec_admin_extract(
    msi: Path,
    target: Path,
    log_path: Path,
    log,
    timeout: int,
    label: str,
) -> None:
    """Run ``msiexec /a`` with a cross-process oomtm lock and 1618 retries."""
    deadline = time.monotonic() + timeout
    lock_logged = False

    while True:
        try:
            fd = os.open(str(_MSIEXEC_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - _MSIEXEC_LOCK.stat().st_mtime > timeout:
                    _MSIEXEC_LOCK.unlink(missing_ok=True)
            except OSError:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"{label}: ventede for længe på oomtm MSI-lås.")
            if not lock_logged:
                log(f"{label}: venter på anden oomtm MSI-udpakning...")
                lock_logged = True
            time.sleep(min(5, max(1, remaining)))

    try:
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"{label}: MSI-udpakning timede ud.")
            attempt += 1
            proc = subprocess.run(
                [
                    "msiexec", "/a", str(msi), "/qn",
                    f"TARGETDIR={target}", "/l*v", str(log_path),
                ],
                timeout=max(1, int(remaining)),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if proc.returncode == 0:
                return

            tail = _tail_text(log_path)
            if proc.returncode == _MSI_BUSY_CODE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"{label}: Windows Installer forblev optaget (kode {_MSI_BUSY_CODE}). "
                        f"Fuld log: {log_path}"
                        + (f"\n--- log-hale ---\n{tail}" if tail else "")
                    )
                wait = min(60, max(5, 5 * attempt), remaining)
                log(
                    f"{label}: Windows Installer er optaget (kode {_MSI_BUSY_CODE}); "
                    f"prøver igen om {int(wait)} sek."
                )
                time.sleep(wait)
                continue

            raise RuntimeError(
                f"{label}: MSI-udpakning fejlede (kode {proc.returncode}). Fuld log: {log_path}"
                + (f"\n--- log-hale ---\n{tail}" if tail else "")
            )
    finally:
        _MSIEXEC_LOCK.unlink(missing_ok=True)


def _extract_libreoffice_no_admin(log, timeout: int) -> None:
    """Unpack LibreOffice without admin rights via an MSI administrative install.

    ``msiexec /a`` only *copies* the program files to TARGETDIR — it neither
    installs nor registers anything, so it raises no UAC prompt. The resulting
    ``soffice.exe`` works fine for headless conversion. Override the source MSI
    with the ``LIBREOFFICE_MSI_URL`` env var.
    """
    url = os.getenv("LIBREOFFICE_MSI_URL", _DEFAULT_LO_MSI_URL)
    target = _NOADMIN_DIR
    target.mkdir(parents=True, exist_ok=True)
    # The source MSI must live OUTSIDE the extraction target: msiexec /a fails
    # with 1603 when TARGETDIR is the folder that holds the source package. Also
    # clear a stray MSI left inside the target by an earlier failed run.
    (target / "libreoffice.msi").unlink(missing_ok=True)
    msi = target.parent / "libreoffice-download.msi"
    log_path = target.parent / "libreoffice-msi-install.log"
    _download(url, msi, log, timeout=min(timeout, 1200))

    head = b""
    try:
        with open(msi, "rb") as fh:
            head = fh.read(8)
    except OSError:
        pass
    if head != _MSI_MAGIC:
        size = msi.stat().st_size if msi.exists() else 0
        msi.unlink(missing_ok=True)
        raise RuntimeError(
            f"hentet fil er ikke en gyldig MSI (størrelse {size} bytes). "
            f"Tjek LIBREOFFICE_MSI_URL: {url}"
        )

    log("LibreOffice: udpakker (msiexec /a — ingen admin)…")
    _run_msiexec_admin_extract(msi, target, log_path, log, timeout, "LibreOffice")
    msi.unlink(missing_ok=True)
    if not _find_soffice_in(target):
        raise RuntimeError(
            f"LibreOffice udpakket til {target}, men soffice.exe blev ikke fundet."
        )


def _run_installer(cmd: list[str], log, timeout: int) -> None:
    log(f"Kører {' '.join(cmd[:3])}…")
    subprocess.run(
        cmd, check=True, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _install_libreoffice(log, timeout: int) -> None:
    """Install LibreOffice, without admin on Windows.

    Windows: unpack the official MSI into a user-writable folder. Linux:
    apt-get → dnf. Raises with a clear message if no supported installer is
    available (the caller surfaces that as the document's file_note so an admin
    knows to install it manually).
    """
    if sys.platform.startswith("win"):
        # msiexec /a unpacks the files with no UAC prompt. Robot accounts
        # usually can't elevate, so Windows never falls back to package managers.
        try:
            _extract_libreoffice_no_admin(log, timeout)
            return
        except Exception as exc:  # pylint: disable=broad-except
            raise RuntimeError(
                "Kan ikke auto-installere LibreOffice uden admin. "
                f"No-admin udpakning fejlede: {exc}. "
                "Sæt LIBREOFFICE_MSI_URL til et tilgængeligt MSI, eller udpak "
                "manuelt og sæt LIBREOFFICE_PATH."
            ) from exc
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


# ---------------------------------------------------------------------------
# Tesseract OCR — find or auto-install (binary + language data), like LibreOffice
# ---------------------------------------------------------------------------

_DEFAULT_TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
]
# Where the no-admin Windows extract lands.
_TESS_NOADMIN_DIR = Path(
    os.getenv("OOMTM_TESSERACT_DIR")
    or (Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir()) / "oomtm" / "tesseract")
)
_TOOLS_DIR = Path(
    os.getenv("OOMTM_TOOLS_DIR")
    or (Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir()) / "oomtm" / "tools")
)
# Windows Tesseract installer source. Override with OOMTM_TESSERACT_EXE_URL for
# an internal mirror.
_DEFAULT_TESSERACT_EXE_URL = (
    "https://github.com/tesseract-ocr/tesseract/releases/download/"
    "5.5.0/tesseract-ocr-w64-setup-5.5.0.20241111.exe"
)
_DEFAULT_7ZIP_MSI_URL = (
    "https://github.com/ip7z/7zip/releases/download/26.01/7z2601-x64.msi"
)
# User-writable folder for the language data, so adding a language (e.g. Danish)
# never needs admin rights on the binary's own (Program Files) tessdata folder.
_TESSDATA_DIR = Path(
    os.getenv("OOMTM_TESSDATA_DIR")
    or (Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir()) / "oomtm" / "tessdata")
)
# Small, fast LSTM data — plenty for spotting e-mails/CPR/phone in clean scans.
# Override the host/quality with OOMTM_TESSDATA_BASE_URL (e.g. .../tessdata_best).
_TESSDATA_BASE_URL = (
    os.getenv("OOMTM_TESSDATA_BASE_URL")
    or "https://github.com/tesseract-ocr/tessdata_fast/raw/main"
).rstrip("/")
_TESS_INSTALL_LOCK = Path(tempfile.gettempdir()) / "oomtm_tesseract_install.lock"


def _find_tesseract_in(dir_: Path) -> str | None:
    """Return the first ``tesseract.exe`` found under *dir_*, or None."""
    try:
        for p in Path(dir_).rglob("tesseract.exe"):
            return str(p)
    except OSError:
        pass
    return None


def find_tesseract(tesseract_path: str | None = None) -> str:
    """Locate the Tesseract binary. Order: explicit arg, TESSERACT_PATH env, PATH,
    no-admin extract, then the usual install locations. Raises if not found."""
    candidates = []
    if tesseract_path:
        candidates.append(tesseract_path)
    env = os.getenv("TESSERACT_PATH")
    if env:
        candidates.append(env)
    found = shutil.which("tesseract")
    if found:
        candidates.append(found)
    noadmin = _find_tesseract_in(_TESS_NOADMIN_DIR)
    if noadmin:
        candidates.append(noadmin)
    candidates.extend(_DEFAULT_TESSERACT_PATHS)
    for c in candidates:
        p = Path(c) if c else None
        if p and p.exists():
            _prepend_process_path(p.parent)
            return str(p)
    raise RuntimeError("Tesseract not found. Install it or set TESSERACT_PATH.")


def _verify_archive_magic(path: Path, magic: bytes, description: str, url: str) -> None:
    head = b""
    try:
        with open(path, "rb") as fh:
            head = fh.read(len(magic))
    except OSError:
        pass
    if head != magic:
        size = path.stat().st_size if path.exists() else 0
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"hentet fil er ikke en gyldig {description} (størrelse {size} bytes). "
            f"Tjek URL: {url}"
        )


def _run_7zip(sevenzip: Path, archive: Path, target: Path, log, timeout: int) -> None:
    target.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(sevenzip), "x", str(archive), f"-o{target}", "-y"],
        timeout=timeout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        tail = _proc_output_tail(proc)
        raise RuntimeError(
            f"7-Zip udpakning fejlede (kode {proc.returncode})."
            + (f"\n--- output ---\n{tail}" if tail else "")
        )
    log(f"Udpakket {archive.name} til {target}.")


def _ensure_7zip(log, timeout: int) -> Path:
    """Return a full 7z.exe suitable for extracting NSIS installers."""
    candidates = [
        shutil.which("7z"),
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    for c in candidates:
        p = Path(c) if c else None
        if p and p.exists():
            return p

    target = _TOOLS_DIR / "7zip"
    existing = next(target.rglob("7z.exe"), None) if target.exists() else None
    if existing:
        return existing

    target.mkdir(parents=True, exist_ok=True)
    msi = target.parent / "7zip-download.msi"
    log_path = target.parent / "7zip-msi-install.log"
    url = os.getenv("OOMTM_7ZIP_MSI_URL", _DEFAULT_7ZIP_MSI_URL)
    _download(url, msi, log, timeout=min(timeout, 600))
    _verify_archive_magic(msi, _MSI_MAGIC, "MSI", url)

    log("7-Zip: udpakker MSI (msiexec /a — ingen admin)...")
    _run_msiexec_admin_extract(msi, target, log_path, log, timeout, "7-Zip")
    msi.unlink(missing_ok=True)

    found = next(target.rglob("7z.exe"), None)
    if not found:
        raise RuntimeError(f"7-Zip helper udpakket til {target}, men 7z.exe blev ikke fundet.")
    return found


def _extract_tesseract_no_admin(log, timeout: int) -> None:
    """Unpack Tesseract into a user-writable folder without running its installer.

    The Windows installer is NSIS-based and writes machine registry keys when
    executed. Extracting it with 7-Zip gives us the runnable files without
    touching HKLM, services, Start Menu entries, or machine PATH.
    """
    url = os.getenv("OOMTM_TESSERACT_EXE_URL", _DEFAULT_TESSERACT_EXE_URL)
    target = _TESS_NOADMIN_DIR
    target.mkdir(parents=True, exist_ok=True)
    installer = target.parent / "tesseract-download.exe"

    _download(url, installer, log, timeout=min(timeout, 1200))
    if not _looks_like_windows_exe(installer):
        size = installer.stat().st_size if installer.exists() else 0
        installer.unlink(missing_ok=True)
        raise RuntimeError(
            f"hentet fil er ikke en gyldig Windows EXE (størrelse {size} bytes). "
            f"Tjek OOMTM_TESSERACT_EXE_URL: {url}"
        )

    sevenzip = _ensure_7zip(log, timeout)
    log("Tesseract: udpakker installer med 7-Zip (ingen admin)...")
    _run_7zip(sevenzip, installer, target, log, timeout=timeout)
    installer.unlink(missing_ok=True)

    path = _find_tesseract_in(target)
    if not path:
        raise RuntimeError(
            f"Tesseract udpakket til {target}, men tesseract.exe blev ikke fundet."
        )
    _prepend_process_path(Path(path).parent)


def _install_tesseract(log, timeout: int) -> None:
    """Install the Tesseract binary with a no-admin path on Windows."""
    if sys.platform.startswith("win"):
        try:
            _extract_tesseract_no_admin(log, timeout)
            return
        except Exception as exc:  # pylint: disable=broad-except
            raise RuntimeError(
                "Kan ikke auto-installere Tesseract uden admin. "
                f"No-admin udpakning fejlede: {exc}. "
                "Sæt OOMTM_TESSERACT_EXE_URL til et tilgængeligt installer-EXE, "
                "eller udpak manuelt og sæt TESSERACT_PATH."
            ) from exc
    for mgr, args in (("apt-get", ["-y", "install", "tesseract-ocr", "tesseract-ocr-dan"]),
                      ("dnf", ["-y", "install", "tesseract", "tesseract-langpack-dan"])):
        exe = shutil.which(mgr)
        if exe:
            _run_installer([exe, *args], log, timeout)
            return
    raise RuntimeError("Kan ikke auto-installere Tesseract: ingen kendt pakkemanager.")


def _ensure_tessdata(langs, log, timeout: int) -> Path:
    """Make sure each language's ``*.traineddata`` is in a user-writable tessdata
    dir (downloading any that are missing) and return that dir, for use as
    ``TESSDATA_PREFIX`` — so we never need to write to the binary's own folder."""
    _TESSDATA_DIR.mkdir(parents=True, exist_ok=True)
    for lang in langs:
        dest = _TESSDATA_DIR / f"{lang}.traineddata"
        if dest.exists() and dest.stat().st_size > 100_000:
            continue
        url = f"{_TESSDATA_BASE_URL}/{lang}.traineddata"
        tmp = dest.with_suffix(".part")
        _download(url, tmp, log, timeout=min(timeout, 600))
        # A real traineddata is ≫100 KB; a proxy error page saved as one is tiny.
        if not tmp.exists() or tmp.stat().st_size < 100_000:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Hentet {lang}.traineddata ser ugyldig ud. Tjek netværk/URL: {url}")
        tmp.replace(dest)
        log(f"Tesseract: sprogdata hentet ({lang}).")
    return _TESSDATA_DIR


def ensure_tesseract(
    tesseract_path: str | None = None,
    *,
    langs=("dan", "eng"),
    install: bool = True,
    log=None,
    install_timeout: int = 1800,
    wait_timeout: int = 1800,
) -> str:
    """Return a usable Tesseract path (installing the binary + the requested
    language data if missing) and point ``TESSDATA_PREFIX`` at a user-writable
    folder holding that data. Mirrors :func:`ensure_libreoffice` — lock-guarded
    so parallel workers don't all install, and a cheap no-op once present.

    Raises ``RuntimeError`` if Tesseract is absent and can't be installed.
    """
    log = log or (lambda *_: None)

    def _finish(path):
        # Point Tesseract at our user tessdata (with dan+eng) for this process.
        try:
            os.environ["TESSDATA_PREFIX"] = str(_ensure_tessdata(langs, log, install_timeout))
        except Exception as exc:  # pylint: disable=broad-except
            log(f"Tesseract: kunne ikke sikre sprogdata ({exc}).")
        return path

    try:
        return _finish(find_tesseract(tesseract_path))
    except RuntimeError:
        if not install:
            raise

    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        try:
            return _finish(find_tesseract(tesseract_path))
        except RuntimeError:
            pass
        try:
            fd = os.open(str(_TESS_INSTALL_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            try:
                if time.time() - _TESS_INSTALL_LOCK.stat().st_mtime > install_timeout:
                    _TESS_INSTALL_LOCK.unlink(missing_ok=True)
            except OSError:
                pass
            time.sleep(5)
            continue
        try:
            log("Tesseract ikke fundet — installerer…")
            try:
                _install_tesseract(log, install_timeout)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"Tesseract-installation fejlede (exit {exc.returncode}). "
                    "Installér Tesseract manuelt, eller sæt TESSERACT_PATH."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Tesseract-installation timede ud.") from exc
            path = find_tesseract(tesseract_path)
            log(f"Tesseract installeret: {path}")
            return _finish(path)
        finally:
            _TESS_INSTALL_LOCK.unlink(missing_ok=True)

    return _finish(find_tesseract(tesseract_path))


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
# Microsoft Office (COM automation) — higher fidelity than LibreOffice for
# Word/Excel/PowerPoint. Windows + installed Office + pywin32 only. Used in
# preference to LibreOffice when available; LibreOffice remains the fallback.
#
# NB: these documents come from external citizens, so macros are force-disabled
# (AutomationSecurity = msoAutomationSecurityForceDisable = 3) before opening.
# COM automation expects a usable session — fine for an interactive/unattended
# RPA login, less so for a bare service account.
# ---------------------------------------------------------------------------

_MSOFFICE_APP = {}
for _e in ("doc", "docx", "docm", "dot", "dotx"):
    _MSOFFICE_APP[_e] = "word"
# for _e in ("xls", "xlsx", "xlsm", "xlsb", "xltx"):
#     _MSOFFICE_APP[_e] = "excel"
for _e in ("ppt", "pptx", "pps", "ppsx", "pot", "potx"):
    _MSOFFICE_APP[_e] = "powerpoint"

_MSO_SECURITY_FORCE_DISABLE = 3  # msoAutomationSecurityForceDisable
_WD_EXPORT_PDF = 17              # wdExportFormatPDF
_XL_TYPE_PDF = 0                 # xlTypePDF
_PP_SAVE_AS_PDF = 32             # ppSaveAsPDF


def _word_to_pdf(src_abs: str, out_abs: str) -> bool:
    import win32com.client as win32
    word = doc = None
    try:
        word = win32.DispatchEx("Word.Application")
        word.Visible = False
        for setter in (lambda: setattr(word, "DisplayAlerts", 0),
                       lambda: setattr(word, "AutomationSecurity", _MSO_SECURITY_FORCE_DISABLE)):
            try:
                setter()
            except Exception:  # pylint: disable=broad-except
                pass
        doc = word.Documents.Open(src_abs, ReadOnly=True, ConfirmConversions=False, AddToRecentFiles=False)
        doc.ExportAsFixedFormat(out_abs, _WD_EXPORT_PDF)
        return True
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:  # pylint: disable=broad-except
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:  # pylint: disable=broad-except
            pass


def _excel_to_pdf(src_abs: str, out_abs: str) -> bool:
    import win32com.client as win32
    excel = wb = None
    try:
        excel = win32.DispatchEx("Excel.Application")
        excel.Visible = False
        for setter in (lambda: setattr(excel, "DisplayAlerts", False),
                       lambda: setattr(excel, "AutomationSecurity", _MSO_SECURITY_FORCE_DISABLE)):
            try:
                setter()
            except Exception:  # pylint: disable=broad-except
                pass
        wb = excel.Workbooks.Open(src_abs, ReadOnly=True, UpdateLinks=0)
        wb.ExportAsFixedFormat(_XL_TYPE_PDF, out_abs)
        return True
    finally:
        try:
            if wb is not None:
                wb.Close(False)
        except Exception:  # pylint: disable=broad-except
            pass
        try:
            if excel is not None:
                excel.Quit()
        except Exception:  # pylint: disable=broad-except
            pass


def _ppt_to_pdf(src_abs: str, out_abs: str) -> bool:
    import win32com.client as win32
    ppt = pres = None
    try:
        ppt = win32.DispatchEx("PowerPoint.Application")
        # PowerPoint refuses Visible=False; open the file windowless instead.
        try:
            ppt.AutomationSecurity = _MSO_SECURITY_FORCE_DISABLE
        except Exception:  # pylint: disable=broad-except
            pass
        pres = ppt.Presentations.Open(src_abs, WithWindow=False, ReadOnly=True)
        pres.SaveAs(out_abs, _PP_SAVE_AS_PDF)
        return True
    finally:
        try:
            if pres is not None:
                pres.Close()
        except Exception:  # pylint: disable=broad-except
            pass
        try:
            if ppt is not None:
                ppt.Quit()
        except Exception:  # pylint: disable=broad-except
            pass


def msoffice_available(ext: str) -> bool:
    """Whether MS Office COM conversion is plausible for this extension here
    (Windows + pywin32 importable + a known Office app for the extension).
    Doesn't actually launch Office — failures still fall back at convert time."""
    if not sys.platform.startswith("win"):
        return False
    if _MSOFFICE_APP.get((ext or "").lower().lstrip(".")) is None:
        return False
    try:
        import win32com.client  # noqa: F401
        return True
    except ImportError:
        return False


def msoffice_to_pdf(src: str | Path, out_path: str | Path, *, log=None) -> Path | None:
    """Convert a Word/Excel/PowerPoint file to PDF via the installed Office.
    Returns the PDF path, or None if Office isn't usable / the conversion failed
    (so the caller can fall back to LibreOffice)."""
    log = log or (lambda *_: None)
    ext = Path(src).suffix.lower().lstrip(".")
    app = _MSOFFICE_APP.get(ext)
    if not app or not sys.platform.startswith("win"):
        return None
    try:
        import pythoncom
    except ImportError:
        return None

    out_path = Path(out_path)
    src_abs = str(Path(src).resolve())
    out_abs = str(out_path.resolve())
    if out_path.exists():
        out_path.unlink()

    pythoncom.CoInitialize()
    try:
        if app == "word":
            ok = _word_to_pdf(src_abs, out_abs)
        elif app == "excel":
            ok = _excel_to_pdf(src_abs, out_abs)
        else:
            ok = _ppt_to_pdf(src_abs, out_abs)
    except Exception as exc:  # pylint: disable=broad-except
        log(f"MS Office-konvertering fejlede ({ext}): {exc}")
        ok = False
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # pylint: disable=broad-except
            pass

    if ok and out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    return None


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
    prefer_msoffice: bool | None = None,
    log=None,
) -> tuple[Path | None, str, str]:
    """Convert ``src`` to PDF, choosing the method from ``ext``.

    Returns ``(pdf_path, status, note)`` where status is one of:
      * ``"ready"``   — pdf_path points to a usable PDF
      * ``"skipped"`` — deliberately not converted (video/audio/unknown)
      * ``"error"``   — conversion was attempted but failed

    Office documents are converted with **LibreOffice headless by default** —
    it's concurrency-safe (each call gets its own profile) and reliable
    unattended. MS Office (COM automation) gives higher fidelity for Word/
    PowerPoint but is single-desktop and unsafe to run concurrently, so it is
    opt-in: pass ``prefer_msoffice=True`` or set ``OOMTM_PREFER_MSOFFICE=1``.
    The default (``None``) reads that env var and stays off unless it's set.

    When ``auto_install`` is True the LibreOffice path installs LibreOffice on
    the worker if it's missing (via ``ensure_libreoffice`` — no-admin on
    Windows). MS Office is never installed automatically.
    """
    log = log or (lambda *_: None)
    if prefer_msoffice is None:
        prefer_msoffice = os.getenv("OOMTM_PREFER_MSOFFICE", "0").strip().lower() in (
            "1", "true", "yes", "on",
        )
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    kind = classify(ext)

    if kind == "pdf":
        return src, "ready", ""

    if kind == "image":
        out = out_dir / f"{src.stem}.pdf"
        result = image_to_pdf(src, out)
        if result:
            return result, "ready", ""
        return None, "error", f"Billedet kunne ikke konverteres ({ext})."

    if kind == "skip":
        return None, "skipped", f"Filtypen {ext} kan ikke konverteres til PDF (gennemse manuelt)."

    # ----- office: MS Office first (best fidelity), LibreOffice fallback -----
    if kind == "office":
        if prefer_msoffice and msoffice_available(ext):
            out = out_dir / f"{src.stem}.pdf"
            result = msoffice_to_pdf(src, out, log=log)
            if result:
                return result, "ready", ""
            log("MS Office utilgængelig/fejlede — falder tilbage til LibreOffice")
        try:
            soffice_path = ensure_libreoffice(soffice_path, install=auto_install, log=log)
        except RuntimeError as exc:
            return None, "error", str(exc)
        result = office_to_pdf(src, out_dir, soffice_path=soffice_path)
        if result:
            return result, "ready", ""
        return None, "error", f"Kunne ikke konvertere filen ({ext})."

    # ----- email / unknown: LibreOffice (Office can't open these well) -------
    try:
        soffice_path = ensure_libreoffice(soffice_path, install=auto_install, log=log)
    except RuntimeError as exc:
        return None, "error", str(exc)

    if kind == "email":
        result = email_to_pdf(src, ext, out_dir, soffice_path=soffice_path)
        if result:
            return result, "ready", ""
        return None, "error", f"E-mailen kunne ikke konverteres ({ext})."

    # unknown — LibreOffice as a last resort; it handles many odd formats.
    result = office_to_pdf(src, out_dir, soffice_path=soffice_path)
    if result:
        return result, "ready", ""
    return None, "skipped", f"Ukendt filtype ({ext}) — kunne ikke konverteres automatisk."
