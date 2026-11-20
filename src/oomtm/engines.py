"""Motorerne bag oomtm.convert: WeasyPrint og LibreOffice - fundet, installeret
uden administrator, og kørt så mange ad gangen, man vil.

HVORFOR TO MOTORER
    LibreOffice er god til det, den er lavet til: Word, Excel, PowerPoint og
    deres åbne søskende. Til HTML er den dårlig - den læste en Outlook-mails
    egen ``<meta charset=iso-8859-1>`` i stedet for vores UTF-8, og æ/ø/å blev
    til "Ã¦"/"Ã˜" (målt 2026-11-20 på en rigtig mail). En mail, en HTML-fil og en
    tekstfil er HTML, når de skal vises, og dem laver WeasyPrint: en HTML/CSS-
    motor til tryk, skrevet i Python (BSD-licens). Begge er fri og åben kildekode.

    Word gennem COM (MS Office) er væk: den klarede ikke flere robotter ad
    gangen på samme maskine.

HVORFOR IKKE CHROMIUM (målt 2026-11-20 på en kommune-pc)
    Chromium gav samme resultat som WeasyPrint på rigtige mails, men tog ~38 s
    pr. PDF: dens program er én usigneret fil på 212 MB, og sikkerheds-
    programmet tjekkede den, hver gang en af dens 8 processer startede (4 s
    hver). WeasyPrint kører i Python og tog 0,1-0,4 s pr. mail; 8 robotter på
    én gang lavede 120 PDF'er på 8,5 s. Til gengæld kan WeasyPrint ikke tegne
    Windows' emoji i farver - de tegnes sort-hvidt med Noto Emoji (oomtm/fonts).

MANGE AD GANGEN
    * WeasyPrint kører i en barneproces pr. robotkørsel (``_weasy_worker``),
      som laver én PDF ad gangen og genbruges. Hænger en side, dræbes den, og
      en ny startes til næste side.
    * LibreOffice får en profilmappe, kun den bruger, mens den kører. Profilerne
      GENBRUGES (``_lo_profil``): at bygge en ny tog 71 s på en kommune-pc, at
      genbruge en 7 s. Hver profil er låst, mens den bruges, så to kørsler
      aldrig deler en. Hænger en kørsel, dræbes HELE procestræet -
      ``soffice.exe`` starter ``soffice.bin`` - og profilen slettes.

INSTALLATION UDEN ADMINISTRATOR
    * LibreOffice: ``oomtm.pdf.ensure_libreoffice`` (``msiexec /a`` pakker
      MSI'en ud i %LOCALAPPDATA%, ingen UAC).
    * WeasyPrint: Python-pakken kommer med ``oomtm[pdf]``. Dens Windows-
      biblioteker (Pango, HarfBuzz, fontconfig) hentes som WeasyPrints egen
      Windows-zip i en FAST version, og kun bibliotekerne og fontconfigs
      indstillinger pakkes ud i %LOCALAPPDATA%\\oomtm\\weasyprint\\<version> -
      ikke weasyprint.exe. Ingen installer, ingen UAC.
    Begge hentes først fra vores egen kopi (mtm-aarhus/oomtm_setup, se dens
    README) og ellers fra originalen, og filen skal passe med en fast SHA-256.

SIKKERHED
    Filerne kommer fra borgere. WeasyPrint har ingen JavaScript, og den må kun
    læse billeder, der ligger i selve siden (``data:``): intet netværk og ingen
    lokale filer. En mail med et sporingsbillede når aldrig internettet fra
    robotmaskinen. LibreOffice kører aldrig makroer i headless-konvertering.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.sax.saxutils import escape as _xml

_LOCAL = Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir())

# ---------------------------------------------------------------------------
# Kørsel med timeout - og hele procestræet dræbt, hvis den hænger
# ---------------------------------------------------------------------------

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _popen_flags() -> dict:
    if sys.platform.startswith("win"):
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen) -> None:
    """Dræb processen og alle dens børn. ``proc.kill()`` alene rammer kun
    forælderen, og soffice.bin lever videre."""
    if proc.poll() is not None:
        return
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=_CREATE_NO_WINDOW, check=False)
    else:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run(cmd: list[str], timeout: float, cwd: str | None = None) -> tuple[int | None, str]:
    """Kør ``cmd``. Returnerer (exitkode, output). Exitkoden er None, hvis den
    blev dræbt efter ``timeout`` sekunder."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd,
                            **_popen_flags())
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out = b""
        try:
            out = proc.stdout.read() if proc.stdout else b""
        except (OSError, ValueError):
            pass
        return None, (out or b"").decode("utf-8", errors="replace")[-2000:]
    return proc.returncode, (out or b"").decode("utf-8", errors="replace")[-2000:]


def _profile_dir(prefix: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{prefix}_{uuid.uuid4().hex}"


def _install_lock(lock: Path, timeout: float, done, install):
    """Fælles for installationer: kun én proces installerer, de andre venter,
    til ``done()`` giver et resultat. ``install()`` kører med låsen."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = done()
        if found:
            return found
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > timeout:
                    lock.unlink(missing_ok=True)
            except OSError:
                pass
            time.sleep(3)
            continue
        try:
            found = done()
            if found:
                return found
            install()
            found = done()
            if not found:
                raise RuntimeError("Installationen blev ikke færdig.")
            return found
        finally:
            lock.unlink(missing_ok=True)
    raise RuntimeError(f"Ventede {timeout:.0f} s på, at en anden proces installerede.")


# ---------------------------------------------------------------------------
# WeasyPrint: bibliotekerne
# ---------------------------------------------------------------------------

#: Skal være den samme version som weasyprint-pakken i pyproject ([pdf]).
#: Ret dem sammen, med vilje - og kør testene. Zip'en skal passe med
#: WEASYPRINT_SHA256, ellers smides den væk. OOMTM_WEASYPRINT_ZIP_URL erstatter
#: begge adresser (fx et internt spejl); hashen tjekkes stadig.
WEASYPRINT_VERSION = "70.0"
WEASYPRINT_SHA256 = "ab1151f210b4e6bb7aa7a79e91a67e8ddb760094c107bfda55241b6aaefe7d53"
_WEASY_ZIP_NAME = "weasyprint-windows-onedir.zip"
_WEASY_MIRROR_URL = ("https://github.com/mtm-aarhus/oomtm_setup/releases/download/"
                     f"weasyprint-{WEASYPRINT_VERSION}/{_WEASY_ZIP_NAME}")
_WEASY_ZIP_URL = ("https://github.com/Kozea/WeasyPrint/releases/download/"
                  f"v{WEASYPRINT_VERSION}/{_WEASY_ZIP_NAME}")

_WEASY_DIR = Path(os.getenv("OOMTM_WEASYPRINT_DIR") or (_LOCAL / "oomtm" / "weasyprint"))
_WEASY_LOCK = Path(tempfile.gettempdir()) / "oomtm_weasyprint_install.lock"
_FONTS = Path(__file__).resolve().parent / "fonts"
_PKG_PARENT = Path(__file__).resolve().parent.parent

# Fra zip'en tages kun bibliotekerne og fontconfigs indstillinger. Ikke
# weasyprint.exe, ikke den Python, den har med, og ikke Windows' egne
# runtime-filer (dem har Windows selv).
_SKIP_DLL = ("python", "vcruntime", "ucrtbase", "api-ms-", "libcrypto", "libssl")
_MARK = "libpango-1.0-0.dll"


def _weasy_libs() -> Path | None:
    d = _WEASY_DIR / WEASYPRINT_VERSION
    if (d / _MARK).is_file() and (d / "etc" / "fonts" / "fonts.conf").is_file():
        return d
    return None


def _download_weasyprint(log, timeout: int) -> None:
    from oomtm.pdf import download_verified
    override = os.getenv("OOMTM_WEASYPRINT_ZIP_URL")
    urls = [override] if override else [_WEASY_MIRROR_URL, _WEASY_ZIP_URL]
    _WEASY_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = _WEASY_DIR / f"weasyprint-{WEASYPRINT_VERSION}-{uuid.uuid4().hex}.zip"
    try:
        download_verified(urls, zip_path, log, timeout=min(timeout, 1200),
                          sha256=WEASYPRINT_SHA256, magic=b"PK\x03\x04")
        # Pak ud i en midlertidig mappe og flyt den på plads til sidst: en
        # afbrudt udpakning må ikke ligne en færdig installation.
        tmp = _WEASY_DIR / f".udpakker-{uuid.uuid4().hex}"
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                navn = info.filename.replace("\\", "/")
                if "/_internal/" not in navn or info.is_dir():
                    continue
                rel = navn.split("/_internal/", 1)[1]
                if ".." in rel.split("/"):
                    continue
                er_dll = ("/" not in rel and rel.lower().endswith(".dll")
                          and not rel.lower().startswith(_SKIP_DLL))
                if not (er_dll or rel.startswith("etc/fonts/")):
                    continue
                ud = tmp / rel
                ud.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(ud, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1 << 20)
        if not (tmp / _MARK).is_file():
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"WeasyPrint-zip'en havde ikke {_MARK}.")
        target = _WEASY_DIR / WEASYPRINT_VERSION
        shutil.rmtree(target, ignore_errors=True)
        tmp.replace(target)
    finally:
        zip_path.unlink(missing_ok=True)


def _fonts_conf(libs: Path | None) -> Path:
    """Vores fontconfig-fil: Windows' skrifter + oomtm/fonts (Noto Emoji), og
    UDEN Segoe UI Emoji. WeasyPrint kan ikke tegne farve-emoji, og med Segoe UI
    Emoji blev de tomme felter i PDF'en; uden den bruges Noto Emoji (sort-hvid).

    Filen hedder noget med sit indhold, så robotter med hver sin oomtm-mappe
    aldrig skriver oven i hinandens."""
    if libs is not None:
        inkluder = libs / "etc" / "fonts" / "fonts.conf"
    else:
        inkluder = Path("/etc/fonts/fonts.conf")
    cache = _WEASY_DIR / "fontcache"
    indhold = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
        "<fontconfig>\n"
        f"  <cachedir>{_xml(str(cache))}</cachedir>\n"
        f"  <dir>{_xml(str(_FONTS))}</dir>\n"
        f'  <include ignore_missing="yes">{_xml(str(inkluder))}</include>\n'
        "  <selectfont><rejectfont><glob>*seguiemj*</glob></rejectfont></selectfont>\n"
        "</fontconfig>\n"
    )
    navn = hashlib.sha1(indhold.encode("utf-8")).hexdigest()[:12]
    conf = _WEASY_DIR / f"fonts-{navn}.conf"
    if not conf.is_file():
        _WEASY_DIR.mkdir(parents=True, exist_ok=True)
        tmp = conf.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(indhold, encoding="utf-8")
        tmp.replace(conf)
    return conf


def ensure_weasyprint(*, install: bool = True, log=None, timeout: int = 1800) -> dict:
    """Gør WeasyPrint klar - henter og pakker bibliotekerne ud, hvis de mangler.
    Sikker at kalde fra mange robotter på én gang: kun én henter, de andre
    venter. Returnerer de miljøvariabler, WeasyPrint skal køre med."""
    log = log or (lambda *_: None)
    try:
        from importlib.metadata import version as _version
        pakke = _version("weasyprint")
    except Exception as exc:  # pylint: disable=broad-except
        raise RuntimeError("Python-pakken weasyprint mangler. Installér oomtm[pdf].") from exc
    if pakke != WEASYPRINT_VERSION:
        log(f"Bemærk: weasyprint-pakken er {pakke}, bibliotekerne er til {WEASYPRINT_VERSION}.")

    libs = None
    if sys.platform.startswith("win"):
        libs = _weasy_libs()
        if libs is None:
            if not install:
                raise RuntimeError("WeasyPrints biblioteker er ikke installeret (ensure_weasyprint).")

            def _install():
                log(f"Henter WeasyPrints biblioteker {WEASYPRINT_VERSION} (ingen admin)…")
                _download_weasyprint(log, timeout)

            libs = _install_lock(_WEASY_LOCK, timeout, _weasy_libs, _install)
            log(f"WeasyPrint klar: {libs}")
    env = {"FONTCONFIG_FILE": str(_fonts_conf(libs))}
    if libs is not None:
        env["WEASYPRINT_DLL_DIRECTORIES"] = str(libs)
        env["FONTCONFIG_PATH"] = str(libs / "etc" / "fonts")
    return env


# ---------------------------------------------------------------------------
# WeasyPrint: barneprocessen
# ---------------------------------------------------------------------------

_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); "
         "from oomtm._weasy_worker import main; main()")
_DOED = object()


class _Arbejder:
    """Én WeasyPrint-proces. Laver én PDF ad gangen."""

    def __init__(self, env: dict, start_timeout: float):
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _BOOT, str(_PKG_PARENT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, **_popen_flags())
        self._linjer: queue.Queue = queue.Queue()
        threading.Thread(target=self._laes, daemon=True).start()
        svar = self._vent(start_timeout)
        if svar is None or svar is _DOED or not svar.get("klar"):
            self.stop()
            grund = ("startede ikke" if svar is None or svar is _DOED
                     else svar.get("fejl", "ukendt fejl"))
            raise RuntimeError(f"WeasyPrint {grund}.")

    def _laes(self) -> None:
        try:
            for linje in self.proc.stdout:
                self._linjer.put(linje)
        except (OSError, ValueError):
            pass
        self._linjer.put(_DOED)

    def _vent(self, timeout: float):
        """Næste svar, ``None`` ved timeout, ``_DOED`` hvis processen døde."""
        try:
            linje = self._linjer.get(timeout=timeout)
        except queue.Empty:
            return None
        if linje is _DOED:
            return _DOED
        return json.loads(linje.decode("utf-8"))

    def lever(self) -> bool:
        return self.proc.poll() is None

    def lav(self, html_fil: Path, pdf_fil: Path, timeout: float) -> tuple[bool, str]:
        try:
            self.proc.stdin.write((json.dumps({"html": str(html_fil), "pdf": str(pdf_fil)})
                                   + "\n").encode("ascii"))
            self.proc.stdin.flush()
        except OSError as exc:
            self.stop()
            return False, f"processen svarede ikke ({exc})"
        svar = self._vent(timeout)
        if svar is None:
            self.stop()
            return False, f"tog mere end {timeout:.0f} s og blev stoppet"
        if svar is _DOED:
            self.stop()
            return False, "processen stoppede uventet"
        return bool(svar.get("ok")), str(svar.get("fejl") or "")

    def stop(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_tree(self.proc)


_ARBEJDER: _Arbejder | None = None
_ARBEJDER_LAAS = threading.Lock()


def close_workers() -> None:
    """Stop WeasyPrint-processen (sker også selv, når robotten slutter)."""
    global _ARBEJDER
    with _ARBEJDER_LAAS:
        if _ARBEJDER is not None:
            _ARBEJDER.stop()
            _ARBEJDER = None


atexit.register(close_workers)


def html_to_pdf(html: str, out_pdf: str | Path, *, timeout: float = 120,
                install: bool = True, log=None) -> Path | None:
    """Lav en HTML-side (tekst) om til en PDF med WeasyPrint. Sidestørrelse og
    margener styres af CSS'en (``@page``). None, hvis det ikke lykkedes."""
    global _ARBEJDER
    log = log or (lambda *_: None)
    out_pdf = Path(out_pdf).resolve()
    out_pdf.unlink(missing_ok=True)
    side = out_pdf.with_name(out_pdf.stem + "-side.html")
    side.write_text(html, encoding="utf-8")
    try:
        with _ARBEJDER_LAAS:
            if _ARBEJDER is None or not _ARBEJDER.lever():
                env = {**os.environ, **ensure_weasyprint(install=install, log=log)}
                # Første start på en maskine bygger fontconfigs liste over
                # Windows' skrifter (10 s på en kommune-pc) - derfor god tid.
                _ARBEJDER = _Arbejder(env, start_timeout=600)
            ok, fejl = _ARBEJDER.lav(side, out_pdf, timeout)
            if not _ARBEJDER.lever():
                _ARBEJDER = None
    finally:
        side.unlink(missing_ok=True)
    if ok and out_pdf.is_file() and out_pdf.stat().st_size > 0:
        return out_pdf
    log(f"WeasyPrint kunne ikke lave siden: {fejl or 'ingen PDF'}")
    out_pdf.unlink(missing_ok=True)
    return None


# ---------------------------------------------------------------------------
# LibreOffice
# ---------------------------------------------------------------------------

_LO_PROFILER = Path(os.getenv("OOMTM_LO_PROFILE_DIR") or (_LOCAL / "oomtm" / "libreoffice-profiler"))
#: Højst så mange LibreOffice-profiler pr. LibreOffice. Er alle i brug, venter
#: en kørsel lidt og får så en engangsprofil, som før.
LO_PROFILE_SLOTS = 8


# En ny profil får Danmark som land, uanset hvad robotmaskinens Windows er sat
# til. Landet styrer, hvordan LibreOffice skriver tal og datoer i et regneark:
# på en pc med danske indstillinger stod 1234.5 som "1234,5" i PDF'en (målt
# 2026-11-21) - en robotbruger med engelsk Windows skal give det samme.
_DANSK_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Setup/L10N"><prop oor:name="ooSetupSystemLocale" oor:op="fuse"><value>da-DK</value></prop></item>
</oor:items>
"""


def _ny_profil(profil: Path) -> None:
    """Læg de danske indstillinger i en profil, LibreOffice ikke har bygget endnu."""
    user = profil / "user"
    if user.exists():
        return
    user.mkdir(parents=True, exist_ok=True)
    (user / "registrymodifications.xcu").write_text(_DANSK_XCU, encoding="utf-8")


def _laas(sti: Path):
    """Lås en fil uden at vente. Windows slipper låsen selv, hvis processen dør."""
    fh = open(sti, "a+b")  # pylint: disable=consider-using-with
    try:
        if sys.platform.startswith("win"):
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def _slip(fh) -> None:
    try:
        if sys.platform.startswith("win"):
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        fh.close()


@contextmanager
def _lo_profil(soffice: str, vent: float = 60):
    """En LibreOffice-profil, kun denne kørsel bruger: (mappe, kassér-funktion).

    Profilerne genbruges mellem kørsler og robotter, én ad gangen hver. Hver
    LibreOffice har sine egne (nøglen er stien til soffice), så en ny version
    aldrig arver en gammel versions profil."""
    noegle = hashlib.sha1(os.path.normcase(str(Path(soffice).resolve())).encode()).hexdigest()[:10]
    base = _LO_PROFILER / noegle
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = None
    deadline = time.monotonic() + vent
    while base is not None:
        for i in range(LO_PROFILE_SLOTS):
            fh = _laas(base / f"plads-{i}.lock")
            if fh is None:
                continue
            profil = base / f"plads-{i}"
            try:
                yield profil, (lambda p=profil: shutil.rmtree(p, ignore_errors=True))
            finally:
                _slip(fh)
            return
        if time.monotonic() > deadline:
            break
        time.sleep(1)
    # Alle profiler i brug (eller ingen plads til dem): en engangsprofil.
    profil = _profile_dir("oomtm_lo")
    try:
        yield profil, (lambda: None)
    finally:
        shutil.rmtree(profil, ignore_errors=True)


def libreoffice_to_pdf(src: str | Path, out_dir: str | Path, *, soffice: str | None = None,
                       timeout: float = 240, export_filter: str = "pdf") -> Path | None:
    """Lav ``src`` om til PDF med LibreOffice headless, i en profil kun den bruger.

    ``export_filter`` er det, der står efter ``--convert-to`` (fx med
    JSON-indstillinger til Calc). None, hvis der ikke kom en PDF ud - også
    når den blev dræbt efter ``timeout``.
    """
    from oomtm.pdf import find_soffice
    exe = soffice or find_soffice()
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    produced = out_dir / (src.stem + ".pdf")
    produced.unlink(missing_ok=True)
    with _lo_profil(exe) as (profil, kasser):
        _ny_profil(profil)
        cmd = [
            exe, "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nologo",
            f"-env:UserInstallation={profil.as_uri()}",
            "--convert-to", export_filter,
            "--outdir", str(out_dir),
            str(src),
        ]
        code, _ = run(cmd, timeout)
        if code != 0:
            # Dræbt eller gået ned midt i: profilen kan være halvt skrevet.
            # Den bygges forfra næste gang.
            kasser()
    if produced.exists() and produced.stat().st_size > 0:
        return produced
    return None
