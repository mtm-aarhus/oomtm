"""Barneprocessen bag ``oomtm.engines.html_to_pdf``. Startes af engines, aldrig
direkte.

Den lever, så længe robotkørslen gør, og laver én PDF ad gangen: en opgave er
én JSON-linje på stdin ({"html": sti, "pdf": sti}), svaret er én JSON-linje på
stdout ({"ok": true} eller {"ok": false, "fejl": "..."}). Hænger en opgave,
dræber engines hele processen og starter en ny - derfor kører WeasyPrint her
og ikke i robottens egen proces.

Miljøet (WEASYPRINT_DLL_DIRECTORIES, FONTCONFIG_FILE) sættes af engines, før
processen starter.
"""
from __future__ import annotations

import json
import os
import sys


def main() -> None:
    # Protokollen får sin egen kopi af stdout. Selve stdout peger på stderr,
    # så intet, der skrives undervejs (fontconfig, advarsler), kan ødelægge den.
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def svar(**kw) -> None:
        proto.write(json.dumps(kw, ensure_ascii=True) + "\n")
        proto.flush()

    try:
        import weasyprint
        from weasyprint.text.fonts import FontConfiguration
        from weasyprint.urls import URLFetcher
    except Exception as exc:  # pylint: disable=broad-except
        svar(klar=False, fejl=f"WeasyPrint kunne ikke indlæses: {exc!r}"[:1000])
        return

    # Kun billeder, der ligger i selve siden (data:). Intet netværk og ingen
    # lokale filer - siderne kommer fra borgere.
    hent = URLFetcher(timeout=5, allowed_protocols={"data"})
    skrifter = FontConfiguration()
    svar(klar=True, version=weasyprint.__version__)

    for linje in sys.stdin:
        if not linje.strip():
            continue
        try:
            job = json.loads(linje)
            with open(job["html"], encoding="utf-8") as fh:
                html = fh.read()
            weasyprint.HTML(string=html, url_fetcher=hent).write_pdf(
                job["pdf"], font_config=skrifter,
                # Mailenes gamle HTML-attributter (width=, bgcolor=, align=)
                # gælder, som i en browser.
                presentational_hints=True)
            svar(ok=True)
        except Exception as exc:  # pylint: disable=broad-except
            svar(ok=False, fejl=repr(exc)[:1000])


if __name__ == "__main__":
    main()
