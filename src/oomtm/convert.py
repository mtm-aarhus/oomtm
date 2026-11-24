"""Én vej til PDF for alle robotter: "kan den laves om?" og "lav den om".

    from oomtm import convert

    if convert.can_convert("Referat.docx"):
        r = convert.convert(sti, arbejdsmappe, log=oc.log_info)
        if r.ok:
            gem(r.pdf)                 # og r.attachments / r.warnings til sagsbehandleren
        else:
            gem_original(sti, note=r.note)

HVORFOR ÉN VEJ
    Før lavede F2, GO, Nova og oomtm hver deres PDF'er af det samme dokument,
    og det kunne ses: forskellige skrifter, sidestørrelser og fejl. Nu går alle
    robotter gennem det her modul, og systemerne bruges kun til at HENTE
    originalen. Samme fil giver samme PDF, uanset hvor den kom fra.

MOTORERNE (oomtm.engines) - alle fri og åben kildekode
    pdf     -> uændret. En PDF laves aldrig om: det er netop den fil,
               overstregningen arbejder på.
    mail    -> .eml/.msg læses (oomtm.mail) og sættes op af WeasyPrint. Kan
               mailens HTML ikke sættes op, laves en PDF af teksten alene,
               med en advarsel.
    html    -> renset og sat op af WeasyPrint.
    tekst   -> .txt/.log/.xml som tekst, sat op af WeasyPrint.
    tabel   -> .csv/.tsv som tabel, sat op af WeasyPrint.
    office  -> Word, Excel, PowerPoint, ODF, RTF, Visio, Publisher: LibreOffice.
    billede -> Pillow (alle sider af en TIFF).

    Alt andet (video, lyd, zip, ukendte filtyper) kan ikke laves om, og
    ``can_convert`` siger nej, før nogen henter filen.

RESULTATET
    ``Result.ok``           der er en PDF (``Result.pdf``)
    ``Result.note``         hvorfor ikke, på dansk - til sagsbehandleren
    ``Result.attachments``  en mails rigtige vedhæftede filer. De kommer IKKE
                            med i PDF'en; billeder i mailens tekst gør.
    ``Result.warnings``     ting i originalen, PDF'en ikke viser: sporede
                            ændringer, kommentarer, skjulte ark.
    ``Result.engine``       hvilken motor (til loggen)
"""
from __future__ import annotations

import csv
import html as html_lib
import io
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from oomtm import engines, mail as mail_mod

PDF_EXTS = frozenset({"pdf"})
MAIL_EXTS = frozenset({"eml", "msg"})
HTML_EXTS = frozenset({"htm", "html", "xhtml"})
TEXT_EXTS = frozenset({"txt", "log", "xml"})
TABLE_EXTS = frozenset({"csv", "tsv"})
OFFICE_EXTS = frozenset({
    "doc", "docx", "docm", "dot", "dotx", "dotm", "odt", "fodt", "ott", "rtf",
    "xls", "xlsx", "xlsm", "xlsb", "xltx", "xltm", "ods", "fods", "ots",
    "ppt", "pptx", "pptm", "pps", "ppsx", "pot", "potx", "odp", "fodp", "otp",
    "vsd", "vsdx", "pub",
})
IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "ico"})

#: Alle filtyper, der kan blive en PDF. KontAKT holder sin egen liste lig den
#: (app/mappe_pdf.py) - en test dér fejler, hvis de skilles.
SUPPORTED = PDF_EXTS | MAIL_EXTS | HTML_EXTS | TEXT_EXTS | TABLE_EXTS | OFFICE_EXTS | IMAGE_EXTS

_KIND = {}
for _kind, _exts in (("pdf", PDF_EXTS), ("mail", MAIL_EXTS), ("html", HTML_EXTS),
                     ("tekst", TEXT_EXTS), ("tabel", TABLE_EXTS), ("office", OFFICE_EXTS),
                     ("billede", IMAGE_EXTS)):
    for _e in _exts:
        _KIND[_e] = _kind

_OOXML = frozenset({"docx", "docm", "dotx", "dotm", "xlsx", "xlsm", "xltx", "xltm",
                    "pptx", "pptm", "ppsx", "potx"})
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _ext(name_or_ext) -> str:
    s = str(name_or_ext or "").strip().lower()
    if "." in s:
        s = s.rsplit(".", 1)[1]
    return s


def file_type(name_or_ext) -> str | None:
    """'pdf', 'mail', 'html', 'tekst', 'tabel', 'office', 'billede' - eller None."""
    return _KIND.get(_ext(name_or_ext))


def can_convert(name_or_ext) -> bool:
    """Kan en fil med dette navn (eller denne filtype) blive en PDF?"""
    return file_type(name_or_ext) is not None


@dataclass
class Result:
    ok: bool
    pdf: Path | None = None
    engine: str = ""
    note: str = ""
    attachments: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sider til WeasyPrint
# ---------------------------------------------------------------------------

_SIDE = """@page {{ size: A4 {retning}; margin: 15mm; }}
html, body {{ margin: 0; padding: 0; background: #fff; }}
body {{ overflow-wrap: anywhere; }}
img {{ max-width: 100% !important; height: auto !important; }}
.oomtm-ekstern {{ display: inline-block; border: 1px dashed #999; color: #555;
                 font-family: Calibri, Carlito, Arial, sans-serif; font-size: 9pt;
                 padding: 1px 4px; margin: 1px 2px; }}
"""


def _side(krop: str, *, css: str = "", retning: str = "portrait", body_style: str = "") -> str:
    return ("<!DOCTYPE html>\n<html lang=\"da\"><head><meta charset=\"utf-8\">"
            f"<style>{_SIDE.format(retning=retning)}</style><style>{css}</style></head>"
            f'<body style="{html_lib.escape(body_style, quote=True)}">{krop}</body></html>')


_TEKST_CSS = """body { font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
                       font-size: 9.5pt; line-height: 1.35; color: #000; }
pre { white-space: pre-wrap; margin: 0; }"""

_TABEL_CSS = """body { font-family: Calibri, Carlito, "Segoe UI", Arial, sans-serif; font-size: 9pt; color: #000; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 0.6px solid #888; padding: 2px 4px; vertical-align: top; text-align: left; }
tr:first-child td { font-weight: bold; background: #eee; }
thead { display: table-header-group; }"""


def _tabel_html(tekst: str, ext: str) -> tuple[str, int]:
    """CSV/TSV som HTML-tabel. (html, antal kolonner)."""
    if ext == "tsv":
        delim = "\t"
    else:
        try:
            delim = csv.Sniffer().sniff(tekst[:20000], delimiters=";,\t|").delimiter
        except csv.Error:
            delim = ";" if tekst[:2000].count(";") >= tekst[:2000].count(",") else ","
    rows = list(csv.reader(io.StringIO(tekst), delimiter=delim))
    cols = max((len(r) for r in rows), default=0)
    e = html_lib.escape
    body = "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table>{body}</table>", cols


def _weasy_side(side: str, out_dir: Path, navn: str, timeout: float, install: bool, log):
    return engines.html_to_pdf(side, out_dir / f"{navn}.pdf", timeout=timeout,
                               install=install, log=log)


# ---------------------------------------------------------------------------
# Office: advarsler om det, PDF'en ikke viser
# ---------------------------------------------------------------------------


def _office_advarsler(src: Path, ext: str) -> list[str]:
    """Sporede ændringer, kommentarer og skjulte ark i en OOXML-fil."""
    if ext not in _OOXML or not zipfile.is_zipfile(src):
        return []
    out = []
    try:
        with zipfile.ZipFile(src) as z:
            names = set(z.namelist())
            if "word/document.xml" in names:
                doc = z.read("word/document.xml")
                if b"<w:ins " in doc or b"<w:del " in doc:
                    out.append("Dokumentet har sporede ændringer. Se originalen efter, før "
                               "den udleveres.")
                if any(n.startswith("word/comments") for n in names):
                    out.append("Dokumentet har kommentarer, som ikke kommer med i PDF'en.")
            if "xl/workbook.xml" in names:
                wb = z.read("xl/workbook.xml")
                if re.search(rb'state="(hidden|veryHidden)"', wb):
                    out.append("Regnearket har skjulte ark, som ikke kommer med i PDF'en.")
                if any(n.startswith(("xl/comments", "xl/threadedComments")) for n in names):
                    out.append("Regnearket har kommentarer, som ikke kommer med i PDF'en.")
            if any(n.startswith("ppt/notesSlides/") for n in names):
                out.append("Præsentationen har noter, som ikke kommer med i PDF'en.")
    except (zipfile.BadZipFile, OSError, KeyError):
        pass
    return out


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------


def _er_pdf(p: Path | None) -> bool:
    if not p or not p.exists() or p.stat().st_size == 0:
        return False
    with open(p, "rb") as fh:
        return fh.read(1024).lstrip().startswith(b"%PDF-")


def ensure_engines(*, install: bool = True, log=None) -> dict:
    """Find (og installér, uden administrator) begge motorer. {motor: sti}.
    Kaldes typisk én gang, når robotten starter, så en manglende motor ses
    med det samme og ikke midt i en sag."""
    from oomtm import pdf as pdf_mod
    env = engines.ensure_weasyprint(install=install, log=log)
    return {"weasyprint": env.get("WEASYPRINT_DLL_DIRECTORIES", "systemets"),
            "libreoffice": pdf_mod.ensure_libreoffice(install=install, log=log)}


def _mail_pdf(m, out_dir: Path, timeout: float, install: bool, log) -> Result:
    """En mail (``oomtm.mail.Mail``) som PDF: hoved og tekst, ellers teksten alene."""
    vedh = list(m.attachments)
    pdf = _weasy_side(mail_mod.to_html(m), out_dir, "mail", timeout, install, log)
    if _er_pdf(pdf):
        return Result(True, pdf, "weasyprint", attachments=vedh)
    # Mailens HTML kunne ikke sættes op (eller tog for lang tid): så
    # hellere teksten alene end ingen PDF.
    pdf = _weasy_side(mail_mod.to_html(m, kun_tekst=True), out_dir, "mail",
                      timeout, install, log)
    if _er_pdf(pdf):
        return Result(True, pdf, "weasyprint", attachments=vedh, warnings=[
            "Mailens opsætning kunne ikke vises, så PDF'en har kun mailens tekst, "
            "uden billeder og formatering. Se originalen efter."])
    return Result(False, engine="weasyprint", note="Mailen kunne ikke laves om til PDF.",
                  attachments=vedh)


def convert_mail(mail, out_dir, *, timeout: float = 300, install: bool = True,
                 log=None) -> Result:
    """En mail, der ikke er en fil, som PDF - med samme hoved som en .eml/.msg.

    Til de mails, et journalsystem gemmer som tekst for sig og hoved for sig:
    en F2-akt har mailens tekst som sit dokument, mens afsender, modtagere, emne
    og dato står på selve akten. Konverteres dokumentet alene, mangler PDF'en
    netop det, der gør den til en mail (2026-11-24: "make sure you add
    to/from/cc/subject to the top of a mail, regardless if it's a mail received
    in kontakt, or a mail fetched from f2/go/nova case").

    Kalderen bygger ``mail`` (``oomtm.mail.Mail``) af systemets egne felter.
    Kaster aldrig for en dårlig mail.
    """
    log = log or (lambda *_: None)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _mail_pdf(mail, out_dir, timeout, install, log)
    except Exception as exc:  # pylint: disable=broad-except
        log(f"oomtm.convert_mail fejlede: {exc!r}")
        return Result(False, engine="mail",
                      note="Mailen kunne ikke laves om til PDF.")


def convert(src, out_dir, *, name: str | None = None, timeout: float = 300,
            install: bool = True, log=None) -> Result:
    """Lav ``src`` om til en PDF i ``out_dir``.

    ``name`` er filens rigtige navn, hvis ``src`` hedder noget andet på disken
    (filtypen afgør motoren). ``install`` lader oomtm installere en manglende
    motor uden administrator. Kaster aldrig for en dårlig fil - en fil, der
    ikke kan laves om, giver ``Result(ok=False, note=…)``.
    """
    from oomtm import pdf as pdf_mod
    log = log or (lambda *_: None)
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = _ext(name or src.name)
    kind = file_type(ext)
    if kind is None:
        hvad = f"Filtypen .{ext}" if ext else "En fil uden filtype"
        return Result(False, note=f"{hvad} kan ikke laves om til PDF.")
    if not src.exists() or src.stat().st_size == 0:
        return Result(False, engine=kind, note="Filen er tom.")

    try:
        if kind == "pdf":
            if _er_pdf(src):
                return Result(True, src, "pdf")
            return Result(False, engine="pdf", note="Filen hedder .pdf, men er ikke en PDF.")

        if kind == "mail":
            m = mail_mod.read_msg(src) if ext == "msg" else mail_mod.read_eml(src)
            return _mail_pdf(m, out_dir, timeout, install, log)

        if kind == "html":
            tekst = mail_mod.decode_html_bytes(src.read_bytes())
            styles, krop, body_style = mail_mod.sanitize_html(tekst)
            pdf = _weasy_side(_side(krop, css=styles, body_style=body_style), out_dir,
                              "side", timeout, install, log)
            return (Result(True, pdf, "weasyprint") if _er_pdf(pdf)
                    else Result(False, engine="weasyprint", note="HTML-filen kunne ikke laves om til PDF."))

        if kind == "tekst":
            tekst = mail_mod.decode_text_bytes(src.read_bytes())
            krop = f"<pre>{html_lib.escape(tekst)}</pre>"
            pdf = _weasy_side(_side(krop, css=_TEKST_CSS), out_dir, "tekst", timeout, install, log)
            return (Result(True, pdf, "weasyprint") if _er_pdf(pdf)
                    else Result(False, engine="weasyprint", note="Tekstfilen kunne ikke laves om til PDF."))

        if kind == "tabel":
            tekst = mail_mod.decode_text_bytes(src.read_bytes())
            krop, cols = _tabel_html(tekst, ext)
            pdf = _weasy_side(_side(krop, css=_TABEL_CSS,
                                    retning="landscape" if cols > 7 else "portrait"),
                              out_dir, "tabel", timeout, install, log)
            return (Result(True, pdf, "weasyprint") if _er_pdf(pdf)
                    else Result(False, engine="weasyprint", note="Tabellen kunne ikke laves om til PDF."))

        if kind == "billede":
            pdf = pdf_mod.image_to_pdf(src, out_dir / "billede.pdf")
            return (Result(True, pdf, "pillow") if _er_pdf(pdf)
                    else Result(False, engine="pillow", note="Billedet kunne ikke læses."))

        # office
        with open(src, "rb") as fh:
            head = fh.read(8)
        if ext in _OOXML and head == _OLE_MAGIC:
            return Result(False, engine="libreoffice",
                          note="Filen er beskyttet med en adgangskode og kan ikke laves om til PDF.")
        soffice = pdf_mod.ensure_libreoffice(install=install, log=log)
        # Et kort, fast navn: LibreOffice kalder resultatet <navn>.pdf, og et
        # langt filnavn fra en borger kan ramme Windows' 260 tegn.
        ind = out_dir / "ind"
        ind.mkdir(exist_ok=True)
        kopi = ind / f"dokument.{ext}"
        shutil.copyfile(src, kopi)
        try:
            pdf = engines.libreoffice_to_pdf(kopi, out_dir, soffice=soffice, timeout=timeout)
        finally:
            shutil.rmtree(ind, ignore_errors=True)
        if _er_pdf(pdf):
            return Result(True, pdf, "libreoffice", warnings=_office_advarsler(src, ext))
        return Result(False, engine="libreoffice",
                      note="Filen kunne ikke laves om til PDF. Den kan være beskadiget "
                           "eller beskyttet med en adgangskode.")
    except Exception as exc:  # pylint: disable=broad-except
        # Den tekniske grund til loggen; sagsbehandleren får en hel sætning.
        log(f"oomtm.convert: {kind} fejlede: {exc!r}")
        return Result(False, engine=kind,
                      note="Filen kunne ikke læses og laves om til PDF. Den kan være beskadiget.")
