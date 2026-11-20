"""oomtm.pdf.image_to_pdf: et billede bliver en PDF - og en TIFF med flere sider
bliver en PDF med ALLE siderne.

Fundet 2026-11-17 under arbejdet med KontAKTMappeToPDF: image_to_pdf gemte kun
første side af en TIFF med flere sider. En scannet brev er tit netop det, og
KontAKTGOToPDF bruger image_to_pdf, når GO selv afviser en fil - så et
dokument i en aktindsigt kunne mangle sider, uden at noget sagde det.

Kør (Pillow i samme version som robotterne, se README "Tests"):
    tests/.venv/Scripts/python.exe tests/test_pdf_images.py
Filen kan også køres med pytest.
"""
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image                                                    # noqa: E402

from oomtm import pdf                                                    # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  <- {extra}" if not cond and extra else ""))
    if not cond:
        failures.append(label)


TMP = Path(tempfile.mkdtemp(prefix="oomtm-billeder-"))


def sider(sti):
    """(antal sider, antal billeder) i en PDF, talt i selve filen."""
    data = Path(sti).read_bytes()
    return (len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", data)),
            len(re.findall(rb"/Subtype\s*/Image", data)))


def tiff(navn, rammer, **kw):
    sti = TMP / navn
    rammer[0].save(sti, save_all=True, append_images=rammer[1:], **kw)
    return sti


def lav(src, navn=None):
    ud = TMP / (navn or (Path(src).stem + ".pdf"))
    return pdf.image_to_pdf(src, ud)


import PIL                                                               # noqa: E402
print(f"Pillow {PIL.__version__}")

print("\n=== TIFF med flere sider ===")
# En scanning: sort/hvid (mode "1"), komprimeret som en scanner gør (CCITT G4).
scan = tiff("scan.tif", [Image.new("1", (850, 1100), 1) for _ in range(3)], compression="group4")
with Image.open(scan) as im:
    check("testfilen er en rigtig TIFF med tre sider", im.format == "TIFF" and im.n_frames == 3)
ud = lav(scan)
check("en sort/hvid scanning med tre sider bliver en PDF", ud is not None and ud.exists())
check("...med tre sider og et billede på hver", ud is not None and sider(ud) == (3, 3),
      ud and sider(ud))

farver = tiff("farver.tif", [Image.new("RGB", (400, 300), "red"),
                             Image.new("RGB", (300, 400), "green"),
                             Image.new("RGB", (200, 200), "blue"),
                             Image.new("RGB", (400, 300), "white")])
ud = lav(farver)
check("fire farvesider i forskellige størrelser: fire sider", ud is not None and sider(ud) == (4, 4),
      ud and sider(ud))

graa = tiff("graa.tif", [Image.new("L", (300, 300), v) for v in (0, 128, 255)])
ud = lav(graa)
check("grå sider: alle tre", ud is not None and sider(ud) == (3, 3), ud and sider(ud))

blandet = tiff("blandet.tif", [Image.new("RGBA", (200, 200), (255, 0, 0, 128)),
                               Image.new("L", (200, 200), 50),
                               Image.new("RGBA", (200, 200), (0, 0, 255, 0))])
ud = lav(blandet)
check("sider med gennemsigtighed (lægges på hvidt): alle tre", ud is not None and sider(ud) == (3, 3),
      ud and sider(ud))

seksten = tiff("seksten.tif", [Image.new("I;16", (100, 100), v) for v in (0, 30000, 65535)])
ud = lav(seksten)
print(f"       (16-bit: {'ingen PDF' if ud is None else sider(ud)})")
check("16-bit grå sider: alle tre - eller ingen PDF, aldrig en med færre sider",
      ud is None or sider(ud) == (3, 3), ud and sider(ud))

mange = tiff("mange.tif", [Image.new("1", (200, 280), i % 2) for i in range(60)], compression="group4")
ud = lav(mange)
check("60 sider: 60 sider", ud is not None and sider(ud) == (60, 60), ud and sider(ud))

print("\n=== det, der skal være som før ===")
en = TMP / "en.tif"
Image.new("RGB", (300, 200), "yellow").save(en)
ud = lav(en)
check("en TIFF med én side: én side", ud is not None and sider(ud) == (1, 1), ud and sider(ud))

gif = TMP / "animation.gif"
rammer = [Image.new("RGB", (50, 50), c) for c in ("red", "green", "blue", "yellow", "white")]
rammer[0].save(gif, save_all=True, append_images=rammer[1:], duration=100, loop=0)
with Image.open(gif) as im:
    check("testfilen er en animeret GIF", im.n_frames == 5)
ud = lav(gif)
check("en animeret GIF er ikke et dokument med sider: første billede", ud is not None
      and sider(ud) == (1, 1), ud and sider(ud))

png = TMP / "logo.png"
Image.new("RGBA", (120, 80), (0, 128, 0, 100)).save(png)
ud = lav(png)
check("en PNG med gennemsigtighed: én side", ud is not None and sider(ud) == (1, 1), ud and sider(ud))

jpg = TMP / "foto.jpg"
Image.new("RGB", (640, 480), "navy").save(jpg)
ud = lav(jpg)
check("et JPEG-foto: én side", ud is not None and sider(ud) == (1, 1), ud and sider(ud))

ødelagt = TMP / "oedelagt.tif"
ødelagt.write_bytes(b"II*\x00" + b"\x00" * 20)
check("en ødelagt TIFF giver None - ingen undtagelse, ingen halv PDF",
      lav(ødelagt) is None)

print("\n=== gennem convert_to_pdf, som robotterne kalder ===")
ud_mappe = TMP / "convert"
resultat, status, note = pdf.convert_to_pdf(scan, "tif", ud_mappe)
check("convert_to_pdf siger 'ready' for scanningen", status == "ready" and resultat is not None,
      (status, note))
check("...og PDF'en har alle tre sider", resultat is not None and sider(resultat) == (3, 3),
      resultat and sider(resultat))


def test_ingen_fejl():
    """Så pytest også kan køre filen."""
    assert not failures, failures


if __name__ == "__main__":
    print()
    if failures:
        print(f"FAILURES: {len(failures)}")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("FAILURES: none")
