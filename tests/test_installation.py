"""LibreOffice installeres én gang pr. maskine - også når Windows nægter at
omdøbe den færdige udpakning.

Fundet 2026-11-24 på en robotmaskine: hver kørsel af KontAKTF2ToPDF hentede og
pakkede LibreOffice ud igen (350 MB, et par minutter), fordi omdøbningen af
``.udpakker-…`` til ``26.2.7.3`` gav "[WinError 5] Adgang nægtet". Så blev hele
udpakningen smidt væk, og næste kørsel startede forfra med samme afslag.

Windows svarer sådan, når en anden proces har en fil i mappen åben (antivirus,
der scanner de nye filer), og når målmappen allerede findes. Testen laver den
rigtige lås: den holder en fil i mappen åben, som en scanner gør - ingen falsk
fejl. msiexec og hentningen er erstattet, så testen tager sekunder.

Kør:
    tests/.venv/Scripts/python.exe tests/test_installation.py
"""
import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from oomtm import pdf  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  <- {extra}" if not cond and extra else ""))
    if not cond:
        failures.append(label)


TMP = Path(tempfile.mkdtemp(prefix="oomtm-installation-"))
V = pdf.LIBREOFFICE_VERSION
os.environ.pop("LIBREOFFICE_PATH", None)
os.environ.pop("LIBREOFFICE_MSI_URL", None)
_ORIG_MOVE = pdf.move_into_place
_ORIG_DOWNLOAD = pdf.download_verified
_ORIG_MSIEXEC = pdf._run_msiexec_admin_extract


def ny_maskine(navn):
    """En tom oomtm\\libreoffice-mappe, som på en ny robotmaskine."""
    d = TMP / navn / "libreoffice"
    d.mkdir(parents=True)
    pdf._NOADMIN_DIR = d
    pdf._PINNED_DIR = d / V
    pdf._INSTALL_LOCK = TMP / navn / "install.lock"
    pdf._MSIEXEC_LOCK = TMP / navn / "msiexec.lock"
    pdf.move_into_place = _ORIG_MOVE
    return d


hentninger = []


def falsk_hent(urls, dest, log, timeout, sha256="", magic=b""):
    hentninger.append(dest)
    Path(dest).write_bytes(pdf._MSI_MAGIC + b"falsk msi")
    return urls[0]


holdte = []


def falsk_msiexec(msi, target, log_path, log, timeout, label, hold=None):
    """Som msiexec /a: program\\soffice.exe og et par andre filer i TARGETDIR.

    hold: sekunder en fil i udpakningen holdes åben bagefter (antivirus, der
    scanner); None = ingen lås, "altid" = låsen slippes først efter testen."""
    (target / "program").mkdir(parents=True, exist_ok=True)
    (target / "program" / "soffice.exe").write_bytes(b"MZ")
    (target / "program" / "mergedlo.dll").write_bytes(b"MZ" + b"\0" * 1000)
    (target / "share" / "registry").mkdir(parents=True, exist_ok=True)
    (target / "share" / "registry" / "main.xcd").write_text("<x/>", encoding="utf-8")
    if hold is None:
        return
    fh = open(target / "program" / "mergedlo.dll", "rb")  # pylint: disable=consider-using-with
    holdte.append(fh)
    if hold != "altid":
        threading.Timer(hold, fh.close).start()


def msiexec_med(hold):
    return lambda *a: falsk_msiexec(*a, hold=hold)


pdf.download_verified = falsk_hent
logs = []
log = logs.append

try:
    # ------------------------------------------------------------------
    print("Windows selv: en åben fil i mappen eller en eksisterende målmappe")
    d = ny_maskine("windows")
    src = d / ".udpakker-x"
    (src / "program").mkdir(parents=True)
    (src / "program" / "soffice.exe").write_bytes(b"MZ")
    with open(src / "program" / "soffice.exe", "rb"):
        try:
            src.replace(d / "maal")
            afvist = False
        except PermissionError:
            afvist = True
    check("omdøbning nægtes, mens en fil i mappen er åben", afvist)
    (d / "maal").mkdir()
    try:
        src.replace(d / "maal")
        afvist = False
    except PermissionError:
        afvist = True
    check("omdøbning nægtes, når målmappen findes", afvist)

    # ------------------------------------------------------------------
    print("remove_tree: skrivebeskyttede filer")
    d = ny_maskine("skrivebeskyttet")
    ro = d / "gammel" / "a" / "b.dll"
    ro.parent.mkdir(parents=True)
    ro.write_bytes(b"x")
    os.chmod(ro, stat.S_IREAD)
    check("rmtree(ignore_errors) lader den ligge (derfor remove_tree)",
          not __import__("shutil").rmtree(d / "gammel", ignore_errors=True) and ro.exists())
    check("remove_tree sletter den", pdf.remove_tree(d / "gammel") and not (d / "gammel").exists())
    check("remove_tree af en mappe, der ikke findes, er True", pdf.remove_tree(d / "findes-ikke"))

    # ------------------------------------------------------------------
    print("move_into_place: venter på, at låsen slippes")
    d = ny_maskine("flyt")
    src = d / ".udpakker-y"
    (src / "program").mkdir(parents=True)
    fh = open(src / "program" / "x.dll", "wb")  # pylint: disable=consider-using-with
    threading.Timer(1.5, fh.close).start()
    logs.clear()
    t0 = time.monotonic()
    pdf.move_into_place(src, d / "maal", log, wait=20)
    check("flyttet, da låsen blev sluppet", (d / "maal" / "program" / "x.dll").is_file() and not src.exists())
    check("ventede på låsen (over 1 sek.)", time.monotonic() - t0 >= 1.0)
    check("én linje i loggen om at vente", sum("prøver igen" in m for m in logs) == 1, logs)

    print("move_into_place: en gammel målmappe skubbes til side og slettes")
    src2 = d / ".udpakker-z"
    (src2 / "program").mkdir(parents=True)
    (src2 / "program" / "ny.dll").write_bytes(b"ny")
    os.chmod(d / "maal" / "program" / "x.dll", stat.S_IREAD)
    pdf.move_into_place(src2, d / "maal", log, wait=5)
    check("ny mappe på plads", (d / "maal" / "program" / "ny.dll").is_file())
    check("gammel mappe væk (også skrivebeskyttet fil)",
          not (d / "maal" / "program" / "x.dll").exists() and not list(d.glob(".gammel-*")))

    print("move_into_place: låsen slippes aldrig - rejser efter wait")
    src3 = d / ".udpakker-w"
    (src3 / "program").mkdir(parents=True)
    fh = open(src3 / "program" / "x.dll", "wb")  # pylint: disable=consider-using-with
    try:
        pdf.move_into_place(src3, d / "maal2", log, wait=1)
        rejst = False
    except OSError:
        rejst = True
    fh.close()
    check("OSError efter wait", rejst)
    check("kilden er urørt", (src3 / "program" / "x.dll").is_file())

    # ------------------------------------------------------------------
    print("_find_soffice_in")
    d = ny_maskine("find")
    check("tom mappe: None", pdf._find_soffice_in(d) is None)
    check("mappe der ikke findes: None", pdf._find_soffice_in(d / "nej") is None)
    (d / ".udpakker-halv" / "program").mkdir(parents=True)
    (d / ".udpakker-halv" / "program" / "soffice.exe").write_bytes(b"MZ")
    check("halv udpakning (punktum-mappe) springes over", pdf._find_soffice_in(d) is None)
    (d / "program").mkdir()
    (d / "program" / "soffice.exe").write_bytes(b"MZ")
    check("program\\soffice.exe findes direkte",
          pdf._find_soffice_in(d) == str(d / "program" / "soffice.exe"))
    (d / "26.0" / "x" / "program").mkdir(parents=True)
    (d / "26.0" / "x" / "program" / "soffice.exe").write_bytes(b"MZ")
    check("dybere layout findes også",
          pdf._find_soffice_in(d / "26.0") == str(d / "26.0" / "x" / "program" / "soffice.exe"))

    # ------------------------------------------------------------------
    print("Installation: intet i vejen")
    d = ny_maskine("normal")
    pdf._run_msiexec_admin_extract = msiexec_med(None)
    hentninger.clear()
    logs.clear()
    sti = pdf.ensure_libreoffice(log=log)
    check("i den låste versions mappe", sti == str(pdf._PINNED_DIR / "program" / "soffice.exe"), sti)
    check("hentet én gang", len(hentninger) == 1)
    check("ingen .udpakker tilbage", not list(d.glob(".udpakker-*")))
    check("klar-filen følger med", (pdf._PINNED_DIR / pdf._KLAR).is_file())
    check("MSI'en er slettet", not (d / "libreoffice-download.msi").exists())
    hentninger.clear()
    check("næste kørsel: samme sti", pdf.ensure_libreoffice(log=log) == sti)
    check("næste kørsel: intet hentet", not hentninger)

    # ------------------------------------------------------------------
    print("Installation: antivirus holder en fil åben i 2 sek.")
    d = ny_maskine("kort-laas")
    pdf._run_msiexec_admin_extract = msiexec_med(2.0)
    hentninger.clear()
    logs.clear()
    sti = pdf.ensure_libreoffice(log=log)
    check("kom på plads alligevel", sti == str(pdf._PINNED_DIR / "program" / "soffice.exe"), sti)
    check("ingen .udpakker tilbage", not list(d.glob(".udpakker-*")))
    check("ingen 'kunne ikke installeres'", not any("kunne ikke installeres" in m for m in logs), logs)

    # ------------------------------------------------------------------
    print("Installation: låsen slippes aldrig (det, robotmaskinen så)")
    d = ny_maskine("altid-laast")
    pdf._run_msiexec_admin_extract = msiexec_med("altid")
    pdf.move_into_place = lambda s, t, lg=None: _ORIG_MOVE(s, t, lg, wait=1)
    # Den gamle, uversionerede udpakning, robotmaskinen faldt tilbage på.
    (d / "program").mkdir()
    (d / "program" / "soffice.exe").write_bytes(b"MZ")
    hentninger.clear()
    logs.clear()
    sti = pdf.ensure_libreoffice(log=log)
    udpak = [p for p in d.glob(f".udpakker-{V}-*")]
    check("én udpakning ligger tilbage", len(udpak) == 1, udpak)
    check("den bruges, hvor den ligger (ikke den gamle)",
          udpak and sti == str(udpak[0] / "program" / "soffice.exe"), sti)
    check("med klar-filen", udpak and (udpak[0] / pdf._KLAR).is_file())
    check("loggen siger, hvor den bruges",
          any("bruges, hvor den ligger" in m for m in logs), logs)
    check("ingen 'kunne ikke installeres'", not any("kunne ikke installeres" in m for m in logs), logs)
    for fh in holdte:
        fh.close()
    holdte.clear()
    hentninger.clear()
    logs.clear()
    sti2 = pdf.ensure_libreoffice(log=log)
    check("næste kørsel: intet hentet", not hentninger, hentninger)
    check("næste kørsel: samme LibreOffice", sti2 == sti)
    check("næste kørsel: ingen installation i loggen", not any("installerer" in m for m in logs), logs)
    check("find_soffice giver også den", pdf.find_soffice() == sti)

    # ------------------------------------------------------------------
    print("Installation: rester fra tidligere kørsler")
    d = ny_maskine("rester")
    pdf._run_msiexec_admin_extract = msiexec_med(None)
    # En halv udpakning (msiexec afbrudt), med en skrivebeskyttet fil.
    halv = d / f".udpakker-{V}-dead0000"
    (halv / "program").mkdir(parents=True)
    (halv / "program" / "soffice.exe").write_bytes(b"MZ")
    os.chmod(halv / "program" / "soffice.exe", stat.S_IREAD)
    # En mappe, der blev skubbet til side, men ikke slettet.
    (d / ".gammel-x" / "a").mkdir(parents=True)
    # En halv målmappe uden soffice.exe.
    (pdf._PINNED_DIR / "share").mkdir(parents=True)
    os.chmod(pdf._PINNED_DIR / "share", stat.S_IREAD)
    (pdf._PINNED_DIR / "readme.txt").write_text("x", encoding="utf-8")
    os.chmod(pdf._PINNED_DIR / "readme.txt", stat.S_IREAD)
    # En anden versions halve udpakning - ikke vores at rydde.
    anden = d / ".udpakker-25.8.0.0-aaaa"
    anden.mkdir()
    logs.clear()
    sti = pdf.ensure_libreoffice(log=log)
    check("installeret i den låste versions mappe",
          sti == str(pdf._PINNED_DIR / "program" / "soffice.exe"), sti)
    check("den halve målmappes filer er væk", not (pdf._PINNED_DIR / "readme.txt").exists())
    check("den halve udpakning er væk", not halv.exists())
    check(".gammel-mappen er væk", not (d / ".gammel-x").exists())
    check("ingen nye .gammel-mapper", not list(d.glob(".gammel-*")))
    check("en anden versions udpakning er urørt", anden.exists())

    # ------------------------------------------------------------------
    print("En færdig udpakning uden klar-fil tæller ikke")
    d = ny_maskine("uden-klar")
    uden = d / f".udpakker-{V}-nnnn"
    (uden / "program").mkdir(parents=True)
    (uden / "program" / "soffice.exe").write_bytes(b"MZ")
    check("find_pinned_soffice: None", pdf.find_pinned_soffice() is None)
    (uden / pdf._KLAR).write_text("x", encoding="utf-8")
    check("med klar-fil: fundet", pdf.find_pinned_soffice() == str(uden / "program" / "soffice.exe"))

finally:
    for fh in holdte:
        fh.close()
    pdf.download_verified = _ORIG_DOWNLOAD
    pdf._run_msiexec_admin_extract = _ORIG_MSIEXEC
    pdf.move_into_place = _ORIG_MOVE
    for root, dirs, files in os.walk(TMP):
        for n in dirs + files:
            try:
                os.chmod(os.path.join(root, n), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
    pdf.remove_tree(TMP)

print()
if failures:
    print(f"{len(failures)} FEJL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("Alle checks ok.")
