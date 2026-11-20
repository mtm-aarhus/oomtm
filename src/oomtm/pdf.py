"""PDF helpers for MTM OO processes: installing LibreOffice and Tesseract
without admin, verified downloads, images to PDF - and the old entry points
``convert_to_pdf``/``office_to_pdf``.

Converting a file to PDF is ``oomtm.convert`` (one way for every robot; see
its docstring). ``convert_to_pdf`` here now calls it, so old callers get the
same PDFs:

* already PDF                 → passthrough
* .msg / .eml / html / text   → oomtm.mail + WeasyPrint (oomtm.engines)
* office                      → LibreOffice headless (oomtm.engines)
* images                      → Pillow (one page; a multi-page TIFF gets every page)
* video / audio / unknown     → skipped (caller marks "kan ikke konverteres")

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
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Extension classification
# ---------------------------------------------------------------------------

# The old names, kept for robots that use them. The lists themselves live in
# oomtm.convert; "office" here means everything else that can become a PDF
# (Office, html, text, csv).
from oomtm.convert import (  # noqa: E402  pylint: disable=wrong-import-position
    HTML_EXTS as _HTML, IMAGE_EXTS as _IMAGE, MAIL_EXTS as _MAIL, OFFICE_EXTS as _OFFICE,
    PDF_EXTS as _PDF, TABLE_EXTS as _TABLE, TEXT_EXTS as _TEXT,
)

PDF_EXTS = set(_PDF)

IMAGE_EXTS = set(_IMAGE)

EMAIL_EXTS = set(_MAIL)

OFFICE_EXTS = set(_OFFICE | _HTML | _TEXT | _TABLE)

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


def find_pinned_soffice() -> str | None:
    """oomtm's own extract of the pinned LIBREOFFICE_VERSION, or None."""
    return _find_soffice_in(_PINNED_DIR)


def find_soffice(soffice_path: str | None = None, *, pinned_only: bool = False) -> str:
    """Locate the LibreOffice binary. Order: explicit arg, LIBREOFFICE_PATH env,
    oomtm's own pinned extract, then (unless ``pinned_only``) PATH, an older
    unversioned oomtm extract and the usual install locations. Raises if not found.

    The pinned extract comes before PATH and Program Files on purpose: the same
    version everywhere is what makes the PDFs the same on every robot.
    """
    candidates = []
    if soffice_path:
        candidates.append(soffice_path)
    env = os.getenv("LIBREOFFICE_PATH")
    if env:
        candidates.append(env)
    candidates.append(find_pinned_soffice())
    if not pinned_only:
        for name in ("soffice", "libreoffice"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
        # An older, unversioned no-admin extract (before LIBREOFFICE_VERSION).
        candidates.append(_find_soffice_in(_NOADMIN_DIR))
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

# The ONE LibreOffice version every robot converts with - pinned, so a document
# comes out the same whichever machine takes it. Bump it on purpose and run the
# tests (tests/test_konvertering.py).
#
# FROM THE ARCHIVE, NOT /stable/. The old pin pointed at
# download.documentfoundation.org/libreoffice/stable/26.2.4/..., and /stable/
# only keeps the CURRENT releases: when 26.2.5 came out, 26.2.4 left /stable/ and
# every new robot machine got a 404 (found 2026-11-20). The archive keeps every
# build for good.
#
# OUR OWN COPY FIRST. The same file is a release asset in mtm-aarhus/oomtm_setup
# (see its README for how to add a version), so an install never depends on an
# address we don't control. The archive is the fallback. Either way the file
# must match LIBREOFFICE_SHA256, or it is thrown away. LIBREOFFICE_MSI_URL
# replaces both (e.g. an internal mirror) - the hash is still checked.
LIBREOFFICE_VERSION = "26.2.7.3"
LIBREOFFICE_SHA256 = "480fecf759fc6393bd614fbf43753eb95a8198fc1a065d41b727e3b664cd1e59"
_LO_MSI_NAME = f"LibreOffice_{LIBREOFFICE_VERSION}_Win_x86-64.msi"
SETUP_MIRROR = "https://github.com/mtm-aarhus/oomtm_setup/releases/download"
_LO_MIRROR_URL = f"{SETUP_MIRROR}/libreoffice-{LIBREOFFICE_VERSION}/{_LO_MSI_NAME}"
_DEFAULT_LO_MSI_URL = (
    "https://downloadarchive.documentfoundation.org/libreoffice/old/"
    f"{LIBREOFFICE_VERSION}/win/x86_64/{_LO_MSI_NAME}"
)

# Where a no-admin extract lands. LOCALAPPDATA is user-writable (no admin).
# Override with OOMTM_LIBREOFFICE_DIR if needed. Each version gets its own
# folder (``_NOADMIN_DIR / LIBREOFFICE_VERSION``), so a version bump installs the
# new one beside the old instead of mixing them - and an older robot on the same
# machine, still pinned to an older oomtm, keeps the one it uses.
_NOADMIN_DIR = Path(
    os.getenv("OOMTM_LIBREOFFICE_DIR")
    or (Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir()) / "oomtm" / "libreoffice")
)
_PINNED_DIR = _NOADMIN_DIR / LIBREOFFICE_VERSION


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
        except urllib.error.HTTPError as exc:
            # 404/410: the file isn't there, and asking again won't change that.
            if exc.code in (404, 410):
                raise
            last_exc = exc
            if attempt < attempts:
                log(f"Download forsøg {attempt}/{attempts} fejlede ({exc}); prøver igen…")
                time.sleep(2 * attempt)
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


def download_verified(urls: list[str], dest: Path, log, timeout: int,
                      sha256: str = "", magic: bytes = b"") -> str:
    """Hent den første adresse i ``urls``, der giver den rigtige fil, til ``dest``.

    Rigtig = begynder med ``magic`` (hvis givet) og har ``sha256`` (hvis givet).
    En adresse, der svarer 404, eller hvis fil ikke passer, springes over, og
    den næste prøves. Returnerer den adresse, der blev brugt. Rejser
    RuntimeError med hver adresses grund, hvis ingen gav den rigtige fil.
    """
    grunde = []
    for url in urls:
        try:
            _download(url, dest, log, timeout=timeout)
        except Exception as exc:  # pylint: disable=broad-except
            grunde.append(f"{url}: {exc}")
            dest.unlink(missing_ok=True)
            continue
        with open(dest, "rb") as fh:
            head = fh.read(len(magic)) if magic else b""
        if magic and head != magic:
            grunde.append(f"{url}: ikke den forventede filtype ({dest.stat().st_size} byte)")
            dest.unlink(missing_ok=True)
            continue
        if sha256:
            faktisk = hashlib.sha256()
            with open(dest, "rb") as fh:
                for blok in iter(lambda: fh.read(1 << 20), b""):
                    faktisk.update(blok)
            if faktisk.hexdigest().lower() != sha256.lower():
                grunde.append(f"{url}: SHA-256 passer ikke ({faktisk.hexdigest()})")
                dest.unlink(missing_ok=True)
                continue
        log(f"Hentet og kontrolleret: {url}")
        return url
    raise RuntimeError("Kunne ikke hente en kontrolleret fil. " + " | ".join(grunde))


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
    override = os.getenv("LIBREOFFICE_MSI_URL")
    urls = [override] if override else [_LO_MIRROR_URL, _DEFAULT_LO_MSI_URL]
    target = _PINNED_DIR
    _NOADMIN_DIR.mkdir(parents=True, exist_ok=True)
    # Unpack into a scratch folder and rename it into place only when it is
    # complete: an interrupted msiexec can leave soffice.exe without its DLLs,
    # and a half extract that LOOKS installed would be found on every later run.
    work = _NOADMIN_DIR / f".udpakker-{LIBREOFFICE_VERSION}-{uuid.uuid4().hex[:8]}"
    # The source MSI must live OUTSIDE the extraction target: msiexec /a fails
    # with 1603 when TARGETDIR is the folder that holds the source package.
    msi = _NOADMIN_DIR / "libreoffice-download.msi"
    log_path = _NOADMIN_DIR / "libreoffice-msi-install.log"
    # An MSI is an OLE file: a mirror's "choose a download" page or an error
    # page served as 200 is rejected by the magic bytes, a wrong file by the hash.
    download_verified(urls, msi, log, timeout=min(timeout, 1200),
                      sha256=LIBREOFFICE_SHA256, magic=_MSI_MAGIC)

    log(f"LibreOffice {LIBREOFFICE_VERSION}: udpakker (msiexec /a - ingen admin)…")
    try:
        work.mkdir(parents=True, exist_ok=True)
        _run_msiexec_admin_extract(msi, work, log_path, log, timeout, "LibreOffice")
        if not _find_soffice_in(work):
            raise RuntimeError(
                f"LibreOffice udpakket til {work}, men soffice.exe blev ikke fundet."
            )
        shutil.rmtree(target, ignore_errors=True)
        work.replace(target)
    finally:
        msi.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


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
    """Return a usable soffice path - the PINNED LIBREOFFICE_VERSION, unpacked
    without admin rights if it isn't there yet.

    An explicit ``soffice_path`` or LIBREOFFICE_PATH wins. Otherwise the pinned
    version is used and installed when missing, even if some other LibreOffice
    is on the machine: the same version on every robot is what makes the PDFs
    the same. Only if that install fails is another LibreOffice used (with a
    log line saying so), rather than not converting at all.

    Safe to call from many parallel workers: installation is guarded by a
    lock file so only one worker installs while the others wait and then pick
    up the freshly-installed binary. A no-op once the pinned version is there,
    so it's cheap to call on every job.

    Raises ``RuntimeError`` if no LibreOffice can be found or installed.
    """
    log = log or (lambda *_: None)
    if soffice_path or os.getenv("LIBREOFFICE_PATH"):
        return find_soffice(soffice_path)
    if find_pinned_soffice():
        return find_soffice()
    if not install:
        return find_soffice()

    try:
        return _install_pinned_libreoffice(log, install_timeout, wait_timeout)
    except RuntimeError as exc:
        try:
            other = find_soffice()
        except RuntimeError:
            raise exc from None
        log(f"LibreOffice {LIBREOFFICE_VERSION} kunne ikke installeres ({exc}). "
            f"Bruger {other} i stedet - PDF'erne kan se lidt anderledes ud end på "
            "de andre robotter.")
        return other


def _install_pinned_libreoffice(log, install_timeout: int, wait_timeout: int) -> str:
    """Install the pinned version under the shared lock; return its soffice."""
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        # Re-check first — another worker may have just finished installing.
        found = find_pinned_soffice()
        if found:
            return find_soffice()
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
            log(f"LibreOffice {LIBREOFFICE_VERSION} ikke fundet - installerer…")
            try:
                _install_libreoffice(log, install_timeout)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"LibreOffice-installation fejlede (exit {exc.returncode}). "
                    "Installér LibreOffice manuelt, eller sæt LIBREOFFICE_PATH."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("LibreOffice-installation timede ud.") from exc
            if not find_pinned_soffice() and sys.platform.startswith("win"):
                raise RuntimeError("LibreOffice blev pakket ud, men findes ikke bagefter.")
            path = find_soffice()
            log(f"LibreOffice installeret: {path}")
            return path
        finally:
            _INSTALL_LOCK.unlink(missing_ok=True)

    raise RuntimeError(f"Ventede for længe på, at en anden installerede LibreOffice "
                       f"{LIBREOFFICE_VERSION}.")


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


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


# Frame modes Pillow's PDF writer stores as they are (bilevel, grey, palette,
# colour, CMYK). Anything else - alpha, 16-bit grey, float - is flattened to RGB
# first. Keeping a bilevel scan bilevel matters: measured on a 3-page A4 scan at
# 300 dpi, the PDF was 7 KB as it is and 534 KB flattened to RGB, because Pillow
# stores RGB pages as JPEG - which also blurs the text.
_PDF_SAFE_MODES = {"1", "L", "P", "RGB", "CMYK"}


def _flatten_rgb(im):
    """One frame as RGB. PDF can't store alpha, so transparency goes onto white."""
    from PIL import Image
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB") if im.mode != "RGB" else im


def image_to_pdf(src: str | Path, out_path: str | Path) -> Path | None:
    """Wrap a raster image into a PDF using Pillow - one page per image, and one
    page per PAGE of a multi-page TIFF.

    MULTI-PAGE TIFF
        A scanned letter is often one TIFF with a page per frame. ``Image.open``
        only gives the first frame, and saving that without ``save_all`` used to
        produce a one-page PDF - the rest of the letter was silently dropped, and
        in an aktindsigt the applicant would get part of a document with nothing
        saying so. Now every frame becomes a page.

        Only TIFF. Other multi-frame formats are not pages: an animated GIF/WebP/
        PNG is an animation, and an MPO from a camera is a photo plus previews.
        Those keep the first frame, as before.

        When every frame is in a mode PDF can hold as it is (the usual case: a
        bilevel or grey scan), Pillow writes the frames straight from the open
        file - no converted copy of every page is kept, and the pages stay
        bilevel (see _PDF_SAFE_MODES). Only a TIFF with frames in another mode
        (alpha, 16-bit) is flattened to RGB, page by page, into memory first.
    """
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
            frames = getattr(im, "n_frames", 1) or 1
            if frames > 1 and (im.format or "").upper() == "TIFF":
                as_is = True
                for i in range(frames):
                    im.seek(i)          # reads the frame's header, not its pixels
                    # A palette with a transparent colour would lose it - flatten.
                    if im.mode not in _PDF_SAFE_MODES or (
                            im.mode == "P" and "transparency" in im.info):
                        as_is = False
                        break
                im.seek(0)
                if as_is:
                    im.save(out_path, "PDF", resolution=150.0, save_all=True)
                else:
                    pages = []
                    for i in range(frames):
                        im.seek(i)
                        pages.append(_flatten_rgb(im.copy()))
                    pages[0].save(out_path, "PDF", resolution=150.0,
                                  save_all=True, append_images=pages[1:])
            else:
                _flatten_rgb(im).save(out_path, "PDF", resolution=150.0)
    except Exception:  # pylint: disable=broad-except
        return None
    return out_path if out_path.exists() and out_path.stat().st_size > 0 else None


# ---------------------------------------------------------------------------
# Den gamle vej ind - nu den samme motor som oomtm.convert
# ---------------------------------------------------------------------------
#
# Robotter, der stadig kalder convert_to_pdf/office_to_pdf, får nu nøjagtig de
# samme PDF'er som dem, der bruger oomtm.convert: mails gennem oomtm.mail og
# WeasyPrint, Office gennem LibreOffice med genbrugte profiler. Word gennem COM
# (MS Office) er fjernet - den klarede ikke flere robotter ad gangen.


def office_to_pdf(
    src: str | Path,
    out_dir: str | Path,
    *,
    soffice_path: str | None = None,
    timeout: int = 240,
) -> Path | None:
    """En Office-fil til PDF med LibreOffice headless (oomtm.engines). None,
    hvis der ikke kom en PDF ud."""
    from oomtm import engines
    return engines.libreoffice_to_pdf(src, out_dir, soffice=find_soffice(soffice_path),
                                      timeout=timeout)


def convert_to_pdf(
    src: str | Path,
    ext: str,
    out_dir: str | Path,
    *,
    soffice_path: str | None = None,  # pylint: disable=unused-argument
    auto_install: bool = False,
    prefer_msoffice: bool | None = None,  # pylint: disable=unused-argument
    log=None,
) -> tuple[Path | None, str, str]:
    """Lav ``src`` om til PDF med ``oomtm.convert`` - filtypen ``ext`` afgør
    motoren. Brug hellere ``oomtm.convert.convert`` direkte i ny kode: den
    giver også en mails vedhæftede filer og advarsler.

    Returnerer ``(pdf, status, note)``, hvor status er
      * ``"ready"``   - ``pdf`` er en brugbar PDF
      * ``"skipped"`` - filtypen kan ikke blive en PDF (video, lyd, zip, ...)
      * ``"error"``   - den kunne, men det lykkedes ikke; ``note`` siger hvorfor

    ``soffice_path`` og ``prefer_msoffice`` bruges ikke længere (MS Office er
    fjernet); de er her, så gamle kald stadig virker.
    """
    from oomtm import convert
    e = (ext or "").lower().lstrip(".")
    if not convert.can_convert(e):
        return None, "skipped", f"Filtypen .{e} kan ikke laves om til PDF." if e else \
            "En fil uden filtype kan ikke laves om til PDF."
    r = convert.convert(src, out_dir, name=f"fil.{e}", install=auto_install, log=log)
    if r.ok:
        return r.pdf, "ready", ""
    return None, "error", r.note
