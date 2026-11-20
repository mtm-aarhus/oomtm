"""oomtm.convert: hver filtype bliver en pæn, rigtig PDF - med æ, ø, å og de
andre tegn, mailens billeder, A4, indlejrede skrifter - og en fil, der ikke kan,
giver en hel sætning til sagsbehandleren i stedet for en fejl.

Testen laver selv sine filer (Word, Excel, PowerPoint, RTF, tekst i tre
tegnsæt, CSV, HTML, mails, billeder, ødelagte filer) og laver dem om med de
rigtige motorer: WeasyPrint og LibreOffice, installeret uden admin, som på en
robotmaskine. Derfor tager første kørsel på en maskine minutter (hentning).

Til sidst:
  * 8 robotter på én gang (8 processer), som når flere sager kører samtidig.
  * tests/ud/index.html: side 1 af hver PDF som billede - til at se efter selv.
  * Ligger der rigtige mails i tests/egne_filer/ (aldrig i git), laves de også.

Kør (samme pakker som robotterne, se README "Tests"):
    tests/.venv/Scripts/python.exe tests/test_konvertering.py
"""
import base64
import html
import io
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import fitz                                                               # noqa: E402  pymupdf
from PIL import Image, ImageDraw                                          # noqa: E402

from oomtm import convert, engines                                        # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  <- {extra}" if not cond and extra else ""))
    if not cond:
        failures.append(label)


TMP = Path(tempfile.mkdtemp(prefix="oomtm-konvertering-"))
IND = TMP / "ind"
IND.mkdir()
UD = ROOT / "tests" / "ud"
UD.mkdir(exist_ok=True)
for gammel in UD.glob("*.png"):
    gammel.unlink()

DANSK = "Ærø Øster Ålborg blåbærgrød æøå ÆØÅ"
TEGN = "€ § ½ ° ± × “citat” – … • é ü ß"
# Tegn, der viser, at tekst er afkodet i det forkerte tegnsæt.
MOJIBAKE = re.compile("Ã[\u0080-\u00bf¦¸¥˜…]|â€|\ufffd")


def png(b, h, farve="#1f4e79", tekst="", mode="RGB"):
    im = Image.new(mode, (b, h), farve)
    if tekst:
        ImageDraw.Draw(im).text((8, 8), tekst, fill="white")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Filerne
# ---------------------------------------------------------------------------

def lav_docx():
    import docx
    from docx.shared import Mm
    d = docx.Document()
    # python-docx' skabelon er amerikansk Letter; et dansk Word-dokument er A4.
    d.sections[0].page_width, d.sections[0].page_height = Mm(210), Mm(297)
    d.add_heading(f"Referat {DANSK}", 1)
    d.add_paragraph(f"Tegn: {TEGN}")
    t = d.add_table(rows=2, cols=3)
    for i, c in enumerate(t.rows[0].cells):
        c.text = f"Kolonne {i + 1} æøå"
    d.add_picture(io.BytesIO(png(400, 120, tekst="BILLEDE I WORD")))
    sti = IND / "referat.docx"
    d.save(sti)
    return sti


def lav_xlsx():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws.append(["Post", "Beløb kr.", "Bemærkning"])
    ws.append(["Blåbær", 1234.5, DANSK])
    ws.append(["Øvrigt", 99, TEGN])
    skjult = wb.create_sheet("Skjult")
    skjult["A1"] = "hemmelig"
    skjult.sheet_state = "hidden"
    sti = IND / "budget.xlsx"
    wb.save(sti)
    return sti


def lav_pptx():
    from pptx import Presentation
    p = Presentation()
    s = p.slides.add_slide(p.slide_layouts[1])
    s.shapes.title.text = f"Præsentation {DANSK}"
    s.placeholders[1].text = f"Tegn: {TEGN}"
    sti = IND / "praesentation.pptx"
    p.save(sti)
    return sti


def lav_rtf():
    tekst = "".join(c if ord(c) < 128 else f"\\u{ord(c) if ord(c) < 32768 else ord(c) - 65536}?"
                    for c in f"RTF {DANSK}")
    sti = IND / "brev.rtf"
    # A4 i twips, som Word skriver det. Uden \paperw/\paperh er en RTF Letter -
    # det siger RTF-standarden, og LibreOffice følger den.
    sti.write_bytes(("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Calibri;}}\\paperw11906\\paperh16838"
                     "\\f0\\fs24 " + tekst + "\\par}").encode("ascii"))
    return sti


def lav_tekster():
    tekst = f"Notat {DANSK}\nTegn: {TEGN}\n" + ("Lang linje uden mellemrum: " + "x" * 300) + "\n"
    ud = []
    for navn, kode in (("notat-utf8.txt", "utf-8"), ("notat-bom.txt", "utf-8-sig"),
                       ("notat-cp1252.txt", "cp1252")):
        sti = IND / navn
        sti.write_bytes(tekst.encode(kode))
        ud.append(sti)
    return ud


def lav_csv():
    smal = IND / "liste.csv"
    smal.write_bytes(f"Navn;By;Beløb\nSøren;Ærøskøbing;1.234,50\nÅse;{DANSK};7\n".encode("cp1252"))
    bred = IND / "bred.csv"
    hoved = ",".join(f"Kolonne {i}" for i in range(1, 11))
    raekke = ",".join(f"Værdi {i} æøå" for i in range(1, 11))
    bred.write_text(f"{hoved}\n{raekke}\n", encoding="utf-8")
    return smal, bred


def lav_html():
    side = (f"<html><head><meta charset='iso-8859-1'><title>t</title>"
            f"<script>alert(1)</script></head><body bgcolor='#ffffee'>"
            f"<h1>Side {DANSK}</h1><p>Tegn: € § ½ °</p>"
            f"<img src='http://example.com/ekstern.png' width='200' height='80' alt='Kort'>"
            f"<img src='http://example.com/spor.gif' width='1' height='1'>"
            f"<img src='file:///C:/Windows/win.ini' width='50' height='50'>"
            f"</body></html>")
    sti = IND / "side.html"
    sti.write_bytes(side.encode("cp1252"))
    return sti


def lav_eml_med_billede():
    m = EmailMessage()
    m["From"] = "Søren Ørsted <soren@example.dk>"
    m["To"] = "Aktindsigt <aktindsigt@aarhus.dk>"
    m["Cc"] = "Åse Ærø <ase@example.dk>"
    m["Subject"] = f"Anmodning om aktindsigt – {DANSK}"
    m["Date"] = format_datetime(datetime(2026, 11, 20, 16, 19, tzinfo=timezone.utc))
    m.set_content(f"Hej\n\n{DANSK}\n")
    m.add_alternative(
        f"<html><body><p>Hej,</p><p>{DANSK}<br>Tegn: {TEGN}</p>"
        f"<p><img src='cid:logo@x' width='120' height='40'></p>"
        f"<p>Med venlig hilsen<br>Søren</p></body></html>", subtype="html")
    m.get_payload()[1].add_related(png(240, 80, "#2e7d32", "LOGO"), "image", "png", cid="<logo@x>")
    m.add_attachment(b"%PDF-1.4 bilag", maintype="application", subtype="pdf",
                     filename="bilag æøå.pdf")
    m.add_attachment(png(50, 50), maintype="image", subtype="png", filename="foto.png")
    sti = IND / "mail-med-billede.eml"
    sti.write_bytes(bytes(m))
    return sti


def lav_eml_latin1():
    # Som en gammel mailklient: quoted-printable i iso-8859-1, emne som encoded-word.
    tekst = f"Hej\r\n{DANSK}\r\nMvh\r\n".encode("iso-8859-1")
    import quopri
    krop = quopri.encodestring(tekst).decode("ascii")
    raw = ("From: =?iso-8859-1?q?S=F8ren?= <s@example.dk>\r\n"
           "To: a@aarhus.dk\r\n"
           "Subject: =?iso-8859-1?q?Bl=E5b=E6rgr=F8d?=\r\n"
           "Date: Thu, 20 Nov 2026 10:00:00 +0100\r\n"
           "MIME-Version: 1.0\r\n"
           "Content-Type: text/plain; charset=iso-8859-1\r\n"
           "Content-Transfer-Encoding: quoted-printable\r\n\r\n" + krop)
    sti = IND / "mail-latin1.eml"
    sti.write_bytes(raw.encode("ascii"))
    return sti


def lav_eml_emoji():
    m = EmailMessage()
    m["From"] = "a@example.dk"
    m["To"] = "b@aarhus.dk"
    m["Subject"] = "Tak 👍"
    m.set_content("Tak for svaret 😀 👍 ✅")
    m.add_alternative("<p>Tak for svaret 😀 👍 ✅</p>", subtype="html")
    sti = IND / "mail-emoji.eml"
    sti.write_bytes(bytes(m))
    return sti


def lav_billeder():
    jpg = IND / "foto.jpg"
    Image.new("RGB", (1200, 800), "#88aacc").save(jpg, "JPEG")
    gennemsigtig = IND / "logo.png"
    Image.new("RGBA", (300, 300), (255, 0, 0, 0)).save(gennemsigtig)
    tif = IND / "scan.tif"
    sider = [Image.new("1", (850, 1100), 1) for _ in range(3)]
    sider[0].save(tif, save_all=True, append_images=sider[1:], compression="group4")
    return jpg, gennemsigtig, tif


def lav_daarlige():
    tom = IND / "tom.docx"
    tom.write_bytes(b"")
    falsk_pdf = IND / "falsk.pdf"
    falsk_pdf.write_bytes(b"det her er ikke en PDF")
    beskyttet = IND / "beskyttet.docx"
    beskyttet.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 4000)
    oedelagt = IND / "oedelagt.docx"
    oedelagt.write_bytes(b"PK\x03\x04" + os.urandom(3000))
    oedelagt_mail = IND / "oedelagt.msg"
    oedelagt_mail.write_bytes(os.urandom(2000))
    video = IND / "film.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return tom, falsk_pdf, beskyttet, oedelagt, oedelagt_mail, video


# ---------------------------------------------------------------------------
# Tjek af en PDF
# ---------------------------------------------------------------------------

def tjek_pdf(navn, r, *, tekst=(), sider=None, liggende=False, billeder=0, emoji=False, a4=True):
    check(f"{navn}: blev en PDF ({r.engine})", r.ok and r.pdf is not None, r.note)
    if not r.ok:
        return None
    d = fitz.open(r.pdf)
    alt = "".join(p.get_text() for p in d)
    for t in tekst:
        check(f"{navn}: teksten har {t!r}", t in alt, alt[:200].replace("\n", " | "))
    check(f"{navn}: ingen forkert afkodede tegn", not MOJIBAKE.search(alt),
          (MOJIBAKE.search(alt) or [""])[0])
    if sider is not None:
        check(f"{navn}: {sider} side(r)", d.page_count == sider, str(d.page_count))
    b, h = d[0].rect.width, d[0].rect.height
    format_ = (595, 842) if not liggende else (842, 595)
    if r.engine != "pillow":
        if a4:
            check(f"{navn}: A4 {'liggende' if liggende else 'stående'}",
                  abs(b - format_[0]) < 3 and abs(h - format_[1]) < 3, f"{b:.0f} x {h:.0f}")
        ikke_indlejret = sorted({f[3] for p in d for f in p.get_fonts() if f[1] == "n/a"})
        check(f"{navn}: alle skrifter er indlejret", not ikke_indlejret, str(ikke_indlejret))
    if billeder:
        n = sum(len(p.get_images()) for p in d)
        check(f"{navn}: har {billeder} billede(r) med", n >= billeder, str(n))
    if emoji:
        skrifter = {f[3].split("+")[-1] for p in d for f in p.get_fonts()}
        check(f"{navn}: emoji tegnet med Noto Emoji", any("Noto" in s for s in skrifter),
              str(sorted(skrifter)))
    for i, p in enumerate(d):
        if i >= 2:
            break
        p.get_pixmap(dpi=55).save(UD / f"{navn}-s{i + 1}.png")
    return d


def lav_om(sti, **kw):
    t = time.monotonic()
    r = convert.convert(sti, TMP / "pdf" / sti.stem, log=print, **kw)
    print(f"       ({sti.name}: {time.monotonic() - t:.1f} s)")
    return r


# ---------------------------------------------------------------------------
# Kør
# ---------------------------------------------------------------------------

print("=== Motorerne findes eller installeres (uden admin) ===")
t = time.monotonic()
motorer = convert.ensure_engines(log=print)
print(f"       {motorer} ({time.monotonic() - t:.0f} s)")
check("WeasyPrint er klar", bool(motorer.get("weasyprint")))
check("LibreOffice er klar", Path(motorer.get("libreoffice") or "").is_file())

print("\n=== Mails ===")
r = lav_om(lav_eml_med_billede())
tjek_pdf("mail-med-billede", r, billeder=1,
         tekst=[DANSK, "€", "½", "Søren Ørsted", "Åse Ærø", "Sendt:",
                "fredag 20. november 2026 17:19", "Vedhæftet:", "bilag æøå.pdf"])
check("mail-med-billede: de to rigtige vedhæftninger gives tilbage",
      sorted(r.attachments) == ["bilag æøå.pdf", "foto.png"], str(r.attachments))
check("mail-med-billede: logoet i teksten er ikke en vedhæftning",
      not any("logo" in a.lower() for a in r.attachments), str(r.attachments))

r = lav_om(lav_eml_latin1())
tjek_pdf("mail-latin1", r, tekst=["Blåbærgrød", "Søren", DANSK], sider=1)

r = lav_om(lav_eml_emoji())
tjek_pdf("mail-emoji", r, tekst=["Tak for svaret"], emoji=True)

print("\n=== Office (LibreOffice) ===")
t1 = time.monotonic()
r = lav_om(lav_docx())
t_foerste = time.monotonic() - t1
tjek_pdf("referat-docx", r, tekst=[DANSK, "€", "Kolonne 1 æøå"], billeder=1)
r = lav_om(lav_xlsx())
# "1234,5": LibreOffice-profilen er sat til Danmark (oomtm.engines._DANSK_XCU).
tjek_pdf("budget-xlsx", r, tekst=["Beløb kr.", "Blåbær", DANSK, "1234,5"])
check("budget-xlsx: advarer om det skjulte ark",
      any("skjulte ark" in w for w in r.warnings), str(r.warnings))
r = lav_om(lav_pptx())
# Et dias beholder sin egen størrelse (her 4:3) - det er ikke A4, og skal ikke være.
tjek_pdf("praesentation-pptx", r, tekst=["Præsentation", "Ærø", "€"], a4=False)
r = lav_om(lav_rtf())
tjek_pdf("brev-rtf", r, tekst=["RTF", "Ærø"])
t2 = time.monotonic()
r = lav_om(lav_docx())
t_anden = time.monotonic() - t2
check("referat-docx anden gang: blev en PDF", r.ok, r.note)
print(f"       LibreOffice: første docx {t_foerste:.1f} s, anden {t_anden:.1f} s "
      "(profilen genbruges)")
profiler = list(engines._LO_PROFILER.glob("*/plads-*/user"))
check("LibreOffice-profilen er gemt til næste gang", bool(profiler), str(engines._LO_PROFILER))

print("\n=== Tekst og tabeller ===")
for sti in lav_tekster():
    r = lav_om(sti)
    tjek_pdf(sti.stem, r, tekst=[DANSK, "€", "½", "xxxxxxxxxx"])
smal, bred = lav_csv()
r = lav_om(smal)
tjek_pdf("liste-csv", r, tekst=["Ærøskøbing", "Søren", "1.234,50"])
r = lav_om(bred)
tjek_pdf("bred-csv", r, tekst=["Kolonne 10", "Værdi 10 æøå"], liggende=True)

print("\n=== HTML ===")
r = lav_om(lav_html())
d = tjek_pdf("side-html", r, tekst=[DANSK, "€", "Kort - ikke med i PDF'en"])
if d is not None:
    alt = "".join(p.get_text() for p in d)
    check("side-html: et lokalt billede (file:) bliver aldrig læst",
          "fonts" not in alt.lower() and sum(len(p.get_images()) for p in d) == 0)

print("\n=== Billeder ===")
jpg, gennemsigtig, tif = lav_billeder()
tjek_pdf("foto-jpg", lav_om(jpg), sider=1)
tjek_pdf("logo-png", lav_om(gennemsigtig), sider=1)
tjek_pdf("scan-tif", lav_om(tif), sider=3)

print("\n=== Filer, der ikke kan blive en PDF ===")
tom, falsk_pdf, beskyttet, oedelagt, oedelagt_mail, video = lav_daarlige()
for sti, forventet in ((tom, "Filen er tom."),
                       (falsk_pdf, "Filen hedder .pdf, men er ikke en PDF."),
                       (beskyttet, "adgangskode"),
                       (oedelagt, "kunne ikke laves om til PDF"),
                       (oedelagt_mail, "kunne ikke læses"),
                       (video, "Filtypen .mp4 kan ikke laves om til PDF.")):
    r = lav_om(sti)
    check(f"{sti.name}: ingen PDF, og en hel sætning til sagsbehandleren",
          not r.ok and forventet in r.note and r.note.endswith("."), r.note)
check("can_convert siger nej til .mp4, .zip og en fil uden filtype",
      not any(convert.can_convert(x) for x in ("film.mp4", "arkiv.zip", "README")))
check("can_convert siger ja til .eml, .docx, .csv, .tif",
      all(convert.can_convert(x) for x in ("a.eml", "b.DOCX", "c.csv", "d.tif")))

print("\n=== En side, der hænger, stoppes - og den næste virker ===")
tung = "<table>" + ("<tr>" + "<td>x æøå</td>" * 30 + "</tr>") * 20000 + "</table>"
t = time.monotonic()
ud = engines.html_to_pdf(tung, TMP / "tung.pdf", timeout=1, log=print)
check("den tunge side gav ingen PDF efter 1 s", ud is None)
check("og blev stoppet hurtigt", time.monotonic() - t < 20, f"{time.monotonic() - t:.0f} s")
ud = engines.html_to_pdf(f"<p>{DANSK}</p>", TMP / "efter.pdf", log=print)
check("næste side virker igen", ud is not None and DANSK in fitz.open(ud)[0].get_text())

# ---------------------------------------------------------------------------
# 8 robotter på én gang
# ---------------------------------------------------------------------------

print("\n=== 8 robotter på én gang ===")
ROBOT = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from oomtm import convert
ind, ud, nr = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
t = time.monotonic()
ok = 0
for f in ("mail-med-billede.eml", "referat.docx", "notat-utf8.txt", "liste.csv"):
    r = convert.convert(ind / f, ud / nr / Path(f).stem)
    ok += bool(r.ok)
print(f"robot {nr}: {ok}/4 PDF'er på {time.monotonic() - t:.1f} s")
sys.exit(0 if ok == 4 else 1)
"""
robot = TMP / "robot.py"
robot.write_text(ROBOT, encoding="utf-8")
t = time.monotonic()
procs = [subprocess.Popen([sys.executable, str(robot), str(ROOT / "src"), str(IND),
                           str(TMP / "parallel"), str(i)]) for i in range(8)]
koder = [p.wait(timeout=1800) for p in procs]
check(f"8 robotter: alle 32 PDF'er lavet ({time.monotonic() - t:.0f} s i alt)",
      koder == [0] * 8, str(koder))
time.sleep(2)
tilbage = subprocess.run(["tasklist", "/FI", "IMAGENAME eq soffice.bin", "/NH"],
                         capture_output=True, text=True).stdout
check("ingen LibreOffice-processer tilbage", "soffice.bin" not in tilbage, tilbage.strip()[:200])

# ---------------------------------------------------------------------------
# Rigtige mails (tests/egne_filer, aldrig i git)
# ---------------------------------------------------------------------------

egne = ROOT / "tests" / "egne_filer"
if egne.is_dir():
    print("\n=== Egne filer (tests/egne_filer) ===")
    for sti in sorted(egne.iterdir()):
        if not convert.can_convert(sti.name):
            continue
        r = lav_om(sti)
        tjek_pdf("egen-" + re.sub(r"\W+", "_", sti.stem).strip("_"), r)
        if r.attachments:
            print(f"       vedhæftet: {r.attachments}")

# ---------------------------------------------------------------------------
# Billeder til at se efter selv
# ---------------------------------------------------------------------------

billeder = sorted(UD.glob("*.png"))
(UD / "index.html").write_text(
    "<!doctype html><meta charset=utf-8><title>oomtm - konverteringer</title>"
    "<style>body{font-family:sans-serif;background:#eee}figure{display:inline-block;"
    "margin:8px;vertical-align:top;background:#fff;padding:6px}img{border:1px solid #999}"
    "</style><h1>Side 1-2 af hver PDF</h1>"
    + "".join(f"<figure><img src='{b.name}'><figcaption>{html.escape(b.stem)}</figcaption>"
              "</figure>" for b in billeder),
    encoding="utf-8")
print(f"\nBilleder af siderne: {UD / 'index.html'}")

engines.close_workers()


def test_ingen_fejl():
    assert not failures, failures


if __name__ == "__main__":
    print(f"\n{len(failures)} fejl" if failures else "\nAlle tjek ok.")
    sys.exit(1 if failures else 0)
