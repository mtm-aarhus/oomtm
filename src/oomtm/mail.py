"""Mails (.eml og .msg) som én HTML-side, klar til at blive printet til PDF.

FUNDET 2026-11-20 på en rigtig mail fra Outlook, konverteret af den gamle vej
(mailen som HTML gennem LibreOffice):

* æ/ø/å blev til "Ã¦"/"Ã˜" - også i vores egen tekst. Outlooks HTML har sin egen
  ``<meta charset=iso-8859-1>``, og LibreOffice troede på den i stedet for på
  vores UTF-8. Her er mailens tekst afkodet til Python-tekst, ALLE mailens egne
  ``<meta>`` er fjernet, og siden skrives som UTF-8 med vores ``<meta>`` først.
* Logoet i signaturen manglede og stod som "vedhæftet fil (ikke medtaget)". Det
  er et billede INDE i teksten (``src="cid:…"``). Her lægges det ind i siden
  som ``data:``, så det kommer med i PDF'en.
* Datoen stod som mailens rå header i UTC. Her står den i dansk tid, på dansk.
* .msg: extract_msg giver hvert tekstfelt med et NUL-tegn til sidst (målt i
  0.55 på en mail fra Outlook) - det fjernes overalt.

VEDHÆFTEDE FILER
    En rigtig vedhæftet fil kommer IKKE med i PDF'en - kun mailens tekst og de
    billeder, der står i den. Navnene gives tilbage (``Mail.attachments``), så
    robotten kan bede sagsbehandleren om at uploade dem for sig, og de står i
    mailens hoved som "Vedhæftet", ligesom når Outlook printer en mail. Ikke
    vedhæftninger: billeder i teksten, en S/MIME-signatur (smime.p7s) og
    Outlooks skjulte filer.

SIKKERHED
    Alt aktivt fjernes (script, iframe, object, embed, form-handlinger,
    on…-attributter, javascript:-links), og billeder udefra hentes aldrig -
    i stedet står der, at der var et. WeasyPrint har desuden ingen JavaScript
    og må kun læse billeder, der ligger i selve siden (oomtm.engines).
"""
from __future__ import annotations

import base64
import html as html_lib
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

DAGE = ("mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag")
MAANEDER = ("januar", "februar", "marts", "april", "maj", "juni", "juli", "august",
            "september", "oktober", "november", "december")

# Filer, der ikke er vedhæftninger, selv om de ligger som en del af mailen.
_IKKE_VEDHAEFTNINGER = {"application/pkcs7-signature", "application/x-pkcs7-signature"}


@dataclass
class Mail:
    subject: str = ""
    sender: str = ""
    to: str = ""
    cc: str = ""
    bcc: str = ""
    date: datetime | None = None
    html: str | None = None
    text: str | None = None
    #: Billeder i teksten: cid (små bogstaver, uden <>) -> (mimetype, bytes)
    images: dict = field(default_factory=dict)
    #: Billeder, der hører til teksten, men som teksten ikke peger på (fx Apple
    #: Mail sætter dem efter teksten) - vises efter teksten.
    loose_images: list = field(default_factory=list)
    #: Rigtige vedhæftede filer - kommer ikke med i PDF'en.
    attachments: list = field(default_factory=list)
    #: Hvad tidspunktet hedder i hovedet. En .eml/.msg har afsendelsestiden;
    #: en mail, der kun kendes fra et journalsystem, har tit kun hvornår den
    #: blev modtaget - så hedder det "Modtaget".
    date_label: str = "Sendt"


def _clean(value) -> str:
    """Tekst uden NUL-tegn og uden luft i enderne. extract_msg giver NUL med."""
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def _cid(value) -> str:
    return _clean(value).strip("<>").strip().lower()


# ---------------------------------------------------------------------------
# Tegnsæt
# ---------------------------------------------------------------------------

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)


def decode_html_bytes(data: bytes, fallback: str | None = None) -> str:
    """HTML-bytes som tekst: BOM, så ``<meta charset>``, så ``fallback`` (fx
    mailens codepage), så UTF-8, og til sidst Windows-1252 - det, en gammel
    dansk fil næsten altid er."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    candidates = []
    m = _META_CHARSET.search(data[:4096])
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    if fallback:
        candidates.append(fallback)
    candidates += ["utf-8", "cp1252"]
    for enc in candidates:
        enc = {"iso-8859-1": "cp1252", "latin1": "cp1252", "latin-1": "cp1252",
               "us-ascii": "cp1252", "ascii": "cp1252"}.get(enc.lower(), enc)
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("cp1252", errors="replace")


def decode_text_bytes(data: bytes) -> str:
    """En tekstfil som tekst: BOM, UTF-8, ellers Windows-1252."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# ---------------------------------------------------------------------------
# .eml
# ---------------------------------------------------------------------------


def _part_text(part) -> str:
    """En tekstdel som tekst - også når dens charset er forkert eller ukendt."""
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, KeyError, AssertionError):
        data = part.get_payload(decode=True) or b""
        if part.get_content_subtype() == "html":
            return decode_html_bytes(data, part.get_content_charset())
        return decode_text_bytes(data)


def read_eml(path: str | Path) -> Mail:
    with open(path, "rb") as fh:
        msg = BytesParser(policy=policy.default).parse(fh)
    mail = Mail()

    def header(name):
        try:
            return _clean(msg.get(name, ""))
        except Exception:  # pylint: disable=broad-except
            raw = msg.get_all(name) or [""]
            return _clean(raw[0])

    mail.subject = header("Subject")
    mail.sender = header("From")
    mail.to = header("To")
    mail.cc = header("Cc")
    mail.bcc = header("Bcc")
    try:
        mail.date = parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
    except (TypeError, ValueError, IndexError):
        mail.date = None

    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    if html_part is not None:
        mail.html = _part_text(html_part)
    if text_part is not None:
        mail.text = _part_text(text_part)
    henvisninger = (mail.html or "").lower()

    for part in msg.walk():
        if part.is_multipart() or part is html_part or part is text_part:
            continue
        ctype = part.get_content_type()
        filename = _clean(part.get_filename())
        if ctype in _IKKE_VEDHAEFTNINGER:
            continue
        if ctype == "message/rfc822":
            indre = part.get_payload()
            emne = ""
            if isinstance(indre, list) and indre:
                emne = _clean(indre[0].get("Subject", ""))
            mail.attachments.append(filename or (emne + ".eml" if emne else "videresendt mail.eml"))
            continue
        cid = _cid(part.get("Content-ID"))
        location = _clean(part.get("Content-Location"))
        disposition = part.get_content_disposition()
        if part.get_content_maintype() == "image":
            data = part.get_payload(decode=True) or b""
            if cid and f"cid:{cid}" in henvisninger:
                mail.images[cid] = (ctype, data)
                continue
            if location and location.lower() in henvisninger:
                mail.images[location.lower()] = (ctype, data)
                continue
            if disposition != "attachment" and not filename:
                mail.loose_images.append((ctype, data))
                continue
            if disposition == "inline" and html_part is None:
                # Apple Mail: et billede i en ren tekstmail står efter teksten.
                mail.loose_images.append((ctype, data))
                continue
        if disposition == "attachment" or filename:
            mail.attachments.append(filename or "uden navn")
        elif part.get_content_maintype() == "text" and mail.text is None:
            mail.text = _part_text(part)
    return mail


# ---------------------------------------------------------------------------
# .msg
# ---------------------------------------------------------------------------


def _msg_codepage(m) -> str | None:
    """Mailens Internet-codepage (PidTagInternetCodepage, 0x3FDE) som Python-navn."""
    for getter in (lambda: m.getPropertyVal("3FDE0003"),
                   lambda: m.props.get("3FDE0003").value):
        try:
            cp = getter()
        except Exception:  # pylint: disable=broad-except
            continue
        if isinstance(cp, int) and cp > 0:
            return {65001: "utf-8", 28591: "cp1252", 1252: "cp1252", 20127: "cp1252",
                    28605: "iso-8859-15", 1200: "utf-16-le"}.get(cp, f"cp{cp}")
    return None


def read_msg(path: str | Path) -> Mail:
    import extract_msg                     # kommer med oomtm[pdf]
    m = extract_msg.openMsg(str(path))
    try:
        mail = Mail()
        mail.subject = _clean(getattr(m, "subject", ""))
        mail.sender = _clean(getattr(m, "sender", ""))
        mail.to = _clean(getattr(m, "to", ""))
        mail.cc = _clean(getattr(m, "cc", ""))
        mail.bcc = _clean(getattr(m, "bcc", ""))
        try:
            d = getattr(m, "date", None)
            mail.date = d if isinstance(d, datetime) else None
        except Exception:  # pylint: disable=broad-except
            mail.date = None
        try:
            html_bytes = m.htmlBody
        except Exception:  # pylint: disable=broad-except
            html_bytes = None
        if html_bytes:
            tekst = (html_bytes if isinstance(html_bytes, str)
                     else decode_html_bytes(html_bytes, _msg_codepage(m)))
            mail.html = tekst.replace("\x00", "")
        try:
            body = m.body
        except Exception:  # pylint: disable=broad-except
            body = None
        if body:
            mail.text = _clean(body) if isinstance(body, str) else decode_text_bytes(body)
        henvisninger = (mail.html or "").lower()

        for att in getattr(m, "attachments", None) or []:
            navn = _clean(getattr(att, "longFilename", None) or getattr(att, "shortFilename", None)
                          or getattr(att, "name", None) or getattr(att, "displayName", None))
            data = getattr(att, "data", None)
            if not isinstance(data, (bytes, bytearray)):
                # En indlejret mail (Outlook: "vedhæft element") - en vedhæftning.
                emne = _clean(getattr(data, "subject", "")) if data is not None else ""
                mail.attachments.append(navn or (emne + ".msg" if emne else "vedhæftet element.msg"))
                continue
            mime = (_clean(getattr(att, "mimetype", None))
                    or mimetypes.guess_type(navn)[0] or "application/octet-stream").lower()
            if mime in _IKKE_VEDHAEFTNINGER:
                continue
            cid = _cid(getattr(att, "cid", None) or getattr(att, "contentId", None))
            if mime.startswith("image/") and cid and f"cid:{cid}" in henvisninger:
                mail.images[cid] = (mime, bytes(data))
                continue
            if getattr(att, "hidden", False):
                # Outlook skjuler sine egne filer (fx et billede, teksten ikke
                # længere bruger). De er ikke en del af det, afsenderen sendte.
                continue
            mail.attachments.append(navn or "uden navn")
        return mail
    finally:
        try:
            m.close()
        except Exception:  # pylint: disable=broad-except
            pass


def read_mail(path: str | Path) -> Mail:
    ext = Path(path).suffix.lower().lstrip(".")
    if ext == "msg":
        return read_msg(path)
    return read_eml(path)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_AKTIVE_TAGS = ("script", "iframe", "frame", "frameset", "object", "embed", "applet",
                "meta", "base", "link", "title", "audio", "video", "source", "track")


def data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def dansk_tid(dt: datetime | None) -> str:
    """'fredag 20. november 2026 17:19' i dansk tid."""
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo("Europe/Copenhagen"))
        except Exception:  # pylint: disable=broad-except
            dt = dt.astimezone(timezone.utc)
            return (f"{DAGE[dt.weekday()]} {dt.day}. {MAANEDER[dt.month - 1]} {dt.year} "
                    f"{dt:%H:%M} (UTC)")
    return f"{DAGE[dt.weekday()]} {dt.day}. {MAANEDER[dt.month - 1]} {dt.year} {dt:%H:%M}"


def sanitize_html(markup: str, images: dict | None = None) -> tuple[str, str, str]:
    """(styles, krop, body-style) fra en HTML-tekst: aktive dele fjernet, billeder
    i teksten lagt ind som ``data:``, billeder udefra erstattet af en tekst.

    ``styles`` er dokumentets egne <style>-blokke, med ``body``/``html`` skrevet
    om til ``.oomtm-krop``, så de ikke rammer vores hoved.
    """
    from bs4 import BeautifulSoup         # kommer med oomtm[pdf]
    images = {k.lower(): v for k, v in (images or {}).items()}
    soup = BeautifulSoup(markup or "", "html.parser")

    styles = []
    for s in soup.find_all("style"):
        css = s.get_text() or ""
        css = re.sub(r"(?<![\w.#-])(html|body)(?![\w-])", ".oomtm-krop", css, flags=re.I)
        css = re.sub(r"@import[^;]*;", "", css, flags=re.I)
        styles.append(css)
        s.decompose()
    for tag in soup.find_all(_AKTIVE_TAGS):
        tag.decompose()

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            v = tag.attrs[attr]
            vs = " ".join(v) if isinstance(v, list) else str(v)
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
            elif attr.lower() in ("href", "src", "action", "formaction", "background", "poster") \
                    and vs.strip().lower().startswith(("javascript:", "vbscript:")):
                del tag.attrs[attr]
        if tag.name == "form":
            tag.attrs.pop("action", None)

    for img in soup.find_all("img"):
        src = str(img.get("src") or "").strip()
        low = src.lower()
        if low.startswith("cid:"):
            cid = low[4:].strip("<>")
            if cid in images:
                mime, data = images[cid]
                img["src"] = data_uri(mime, data)
                continue
        elif low.startswith("data:"):
            continue
        elif low in images:                       # Content-Location
            mime, data = images[low]
            img["src"] = data_uri(mime, data)
            continue
        # Udefra (http/https/file) eller et cid, der ikke findes: aldrig hentet.
        # Et sporingsbillede (1x1) fjernes helt - det er ikke indhold.
        try:
            b, h = int(str(img.get("width") or 99)), int(str(img.get("height") or 99))
        except ValueError:
            b, h = 99, 99
        if b <= 2 or h <= 2:
            img.decompose()
            continue
        alt = _clean(img.get("alt"))
        erstatning = soup.new_tag("span", attrs={"class": "oomtm-ekstern"})
        erstatning.string = f"[Billede{': ' + alt if alt else ''} - ikke med i PDF'en]"
        img.replace_with(erstatning)

    body = soup.body
    body_style = ""
    if body is not None:
        body_style = str(body.get("style") or "")
        if body.get("bgcolor"):
            body_style += f";background-color:{body.get('bgcolor')}"
        krop = body.decode_contents()
    else:
        for t in soup.find_all(("html", "head")):
            t.unwrap()
        krop = str(soup)
    return "\n".join(styles), krop, body_style


SIDE_CSS = """
@page { size: A4; margin: 16mm 15mm 18mm 15mm; }
html, body { margin: 0; padding: 0; background: #fff; }
body { font-family: Calibri, Carlito, "Segoe UI", Arial, Helvetica, sans-serif;
       font-size: 11pt; color: #000; line-height: 1.3; }
.oomtm-emne { font-size: 15pt; font-weight: bold; margin: 0 0 8px; overflow-wrap: anywhere; }
table.oomtm-hoved { border-collapse: collapse; width: 100%; font-size: 10pt; margin: 0; }
table.oomtm-hoved th { text-align: left; vertical-align: top; font-weight: bold;
                       padding: 1px 14px 1px 0; white-space: nowrap; width: 1%; }
table.oomtm-hoved td { padding: 1px 0; overflow-wrap: anywhere; }
hr.oomtm-streg { border: 0; border-top: 1.5px solid #000; margin: 8px 0 14px; }
.oomtm-krop { overflow-wrap: anywhere; }
.oomtm-krop img { max-width: 100% !important; height: auto !important; }
.oomtm-krop table { max-width: 100%; }
.oomtm-krop pre { white-space: pre-wrap; }
.oomtm-tekst { white-space: pre-wrap; font-family: inherit; margin: 0; }
.oomtm-ekstern { display: inline-block; border: 1px dashed #999; color: #555;
                 font-size: 9pt; padding: 1px 4px; }
.oomtm-billeder img { display: block; margin: 10px 0; max-width: 100%; height: auto; }
"""


def _tekst_af_html(markup: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(markup or "", "html.parser")
    for tag in soup.find_all(("style", "script", "head", "title")):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    tekst = soup.get_text("\n")
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", tekst).strip()


def to_html(mail: Mail, *, kun_tekst: bool = False) -> str:
    """Mailen som én HTML-side (UTF-8): emne, hoved og tekst.

    ``kun_tekst`` giver mailens tekst alene, uden dens HTML og billeder - til
    når HTML'en ikke kan sættes op."""
    e = html_lib.escape
    raekker = [("Fra", mail.sender), (mail.date_label or "Sendt", dansk_tid(mail.date)),
               ("Til", mail.to),
               ("Cc", mail.cc), ("Bcc", mail.bcc), ("Emne", mail.subject),
               ("Vedhæftet", "; ".join(mail.attachments))]
    hoved = "".join(f"<tr><th>{e(k)}:</th><td>{e(v)}</td></tr>" for k, v in raekker if v)

    styles, body_style = "", ""
    if kun_tekst:
        tekst = mail.text if (mail.text or "").strip() else _tekst_af_html(mail.html or "")
        krop = f'<div class="oomtm-tekst">{e(tekst or "")}</div>'
    elif mail.html and mail.html.strip():
        styles, krop, body_style = sanitize_html(mail.html, mail.images)
    else:
        krop = f'<div class="oomtm-tekst">{e(mail.text or "")}</div>'
    if mail.loose_images and not kun_tekst:
        krop += '<div class="oomtm-billeder">' + "".join(
            f'<img src="{data_uri(mime, data)}">' for mime, data in mail.loose_images) + "</div>"

    return (
        "<!DOCTYPE html>\n<html lang=\"da\"><head><meta charset=\"utf-8\">"
        f"<style>{SIDE_CSS}</style><style>{styles}</style></head><body>"
        f'<div class="oomtm-emne">{e(mail.subject or "(intet emne)")}</div>'
        f'<table class="oomtm-hoved">{hoved}</table><hr class="oomtm-streg">'
        f'<div class="oomtm-krop" style="{e(body_style, quote=True)}">{krop}</div>'
        "</body></html>"
    )
