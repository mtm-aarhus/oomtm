"""F2REST: KontAKTs klient til cBrains ESDH.

F2 ERSTATTER GO. Der er ikke noget GO tilbage i KontAKT.

FILEN LIGGER HER, FORDI BEGGE SIDER BRUGER DEN. Webtieren søger i F2 med den (se
f2.py ved siden af), og robotterne journaliserer med den.

DENNE FIL ER ORIGINALEN. Robotterne importerer den som ``oomtm.f2`` - biblioteket
er pip-installeret på robotmaskinerne, så de skal ikke have hver sin kopi - og
``oomtm/src/oomtm/f2.py`` er derfor en kopi af denne fil, byte for byte.
``tests/test_copies.py`` fejler, hvis de er kommet ud af takt.

Hvorfor originalen ligger her og ikke i biblioteket: alt, hvad vi VED om F2, er
målt her. Testene (``tests/test_f2_rest.py``, 76 checks) og protokollen over
hver måling (``F2-TESTLOG.md``) ligger i dette repo, og det er her, filen bliver
ved med at blive rettet. Ret her, og kopiér ud til oomtm igen.

Modulet importerer med vilje ikke noget fra ``app``: så kan det kopieres som det
er. Konfigurationen kommer derfor ind udefra - fra OpenOrchestrator i robotterne
(``Config.from_orchestrator``) og fra Flask-konfigurationen i webtieren
(``f2.py``).

HELE API'ET OPDAGES VED KØRSEL
    Kun én URL er hårdkodet: ``/F2Rest/ServiceIndex``. Den er åben (intet login)
    og indeholder link-relationer til resten. Alt andet - hvor man opretter en
    sag, hvor man søger, hvor token-endpointet er - læses derfra, og videre links
    læses fra de svar man får. Det er ikke pedanteri: cBrain skriver eksplicit,
    at URL-strukturen ikke er dokumenteret og kan ændre sig, og at klienten SKAL
    opdage links ved kørsel.

LOGIN HAR TRE DELE, OG DEN MIDTERSTE ER LET AT OVERSE
    * basic auth  = F2REST-klientens id og hemmelighed (OO-credentialet)
    * username    = navnet på den F2-BRUGER vi handler som
    * password    = valgfrit, og vi bruger det ikke

    Uden ``username`` svarer serveren 401 ``invalid_client - User not enabled for
    this client``. cBrain har koblet vores F2REST-klient til en F2-bruger; i AAK's
    testmiljø har de samme navn, og det er derfor standarden nedenfor. Skal de
    engang være forskellige, er det den ene ekstra værdi i konfigurationen.

POST ONCE EXACTLY (PEO) - HVORFOR ALT OPRETTES I TO TRIN
    Man beder først om en engangs-URL, og POSTer så data til DEN. Pointen er, at
    trin 2 kan gentages i det uendelige uden at oprette to sager: første gang
    svarer den 201, hver gang derefter 303 med ``Location`` til den forekomst,
    der allerede blev lavet. Det er MÅLT (2026-09-07), og det er grunden til, at
    en robot kan køre det samme køelement igen uden at journalisere dobbelt.

    Bemærk at trin 1 svarer **201**, ikke 303 som dokumentationens side 11
    påstår. ``poe()`` tager begge.

DE MÅLTE FALDGRUBER
    Alt herunder er prøvet mod aak-esdh-test-mob, ikke læst. Se F2-TESTLOG.md.

    SKRIVERETTEN PÅ EN AKT har to betingelser, og de skal begge være opfyldt.
        Målt på 42 akter fordelt på 12 sager, med ``PATCH /Title`` som prøve:

            AccessLevel=Unit  + ansvarlig er en ENHED    15 af 15 lykkedes
            AccessLevel=Unit  + ansvarlig er en PERSON    2 af 4
            AccessLevel=All                               0 af 16
            AccessLevel=Involved                          0 af 7

        Så ``AccessLevel`` SKAL være ``Unit`` (se ``ACCESS_LEVEL``), OG den
        ansvarlige skal være en **enhed**. Med en person som ansvarlig er det
        ikke til at regne med - to akter med den ene kollega gav 200, to med en
        anden gav 403, og hvad der skiller dem, ved vi ikke.

        Det er grunden til, at ``create_matter`` kræver afdelingen som
        ansvarlig. Journaliseringen er en afstemning, der skal kunne rette sig
        selv; en akt, vi ikke kan røre, er tabt for altid. Menneskene kommer til
        via ``InvolvedParties``, som lægger sig oven på enhedsadgangen.

    ``DocumentsLocked`` AFVISER NYE DOKUMENTER, OG KAN IKKE LÅSES OP.
        En låst akt svarer ``400 Du har ikke rettigheder til at oprette
        dokumentet``. ``rel/set-documents-locked`` virker ikke: 400 på
        ``locked``/``Locked`` og 500 ``The operation SetMatterDocumentsLocked
        for MatterHandler is not ready``.

        Hvad der låser: **alle** ``Inbound``-akter kommer tilbage låste (og
        arkiverede), og en ``Outbound``-akt oprettet MED ``SentDate`` blev også
        låst, mens ``Outbound`` uden ``SentDate`` ikke blev. Derfor sætter
        journaliseringen ikke ``SentDate`` på udleveringsakten - og fordi den
        regel er målt og ikke oplyst, spørger den alligevel ``locked()`` og
        lægger en ny akt på, hvis den gamle er låst. Se ``locked``.

    ``CaseMatterNumber`` - AKTNUMMERET - FÅS FØRST VED ARKIVERING. Målt på de
        samme 42 akter: hver akt med ``Archived=true`` har et nummer, ingen med
        ``Archived=false`` har et. Det er nummeret, en sagsbehandler ser i F2.

    ET DOKUMENT KAN IKKE FJERNES. Det har slet ingen slette-relation - kun
        ``content``, ``pdf-content``, ``pdf-content-flattened``,
        ``text-content``, ``upload-validation``, ``self``, ``up``, ``alternate``.
        Titlen kan derimod PATCHes, og det er hvad ``mark_superseded`` bruger:
        et dokument, der er trukket ud af udleveringen, mærkes i stedet for at
        forsvinde. Aktlisten - som også journaliseres - er det autoritative
        indeks over, hvad der faktisk blev udleveret.

    F2 RENDERER SELV TIL PDF. ``rel/pdf-content`` lavede en 52-byte text/plain
        om til en rigtig PDF. Det betyder, at ingen robot behøver LibreOffice for
        at hente et dokument som PDF - se ``pdf_content``.

    ``ExternalId`` kan kun sættes med PATCH, ikke ved oprettelsen. Til gengæld
        kan der søges på den, og det er vores idempotensnøgle: "hvilken F2-sag
        hører KontAKT-sag 35 til" er ét GET.

    SØGEINDEKSET ER FORSINKET med minutter. Lige efter en oprettelse giver ALLE
        søgninger 0 træf - også på sagens eget nummer. Derfor returnerer
        ``create_case`` sagen selv, og kalderen skal gemme id og nummer med det
        samme. Søgning er en reparationsvej, ikke et opslag.

    En ``PartyReference`` er ikke et navn. ``{"Name": ...}`` alene giver
        ``Could not find responsible party``. Brug ``party()`` nedenfor.

    SLETNING svarer 204 uden at gøre noget, hvis man ikke har lov. Kun dry-run
        siger sandheden (403). ``delete_case`` verificerer derfor bagefter og
        lyver ikke om udfaldet.
"""
from __future__ import annotations

import csv
import io
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable, Optional

import requests
from requests.auth import HTTPBasicAuth

NS = {"cb": "http://cbrain.com/casefile/schema/"}
REL = "http://cbrain.com/casefile/rel/"

# Den ene hårdkodede sti. Alt andet opdages.
SERVICE_INDEX = "/F2Rest/ServiceIndex"

# Se faldgruberne i modulets docstring: dette er det eneste niveau, robotten kan
# blive ved med at skrive i. Det er også netop det niveau, der giver den
# ansvarliges enhed adgang - så det ene rigtige valg er også det, der giver
# GO-lignende opførsel. Der er ingen grund til at kunne vælge et andet.
ACCESS_LEVEL = "Unit"

# Aktindsigtssagens journalisering. KLE-NUMRE og ikke GUID'er, og det er en
# vigtig detalje: GUID'erne er installationsspecifikke og ville skulle skiftes
# ved overgangen til produktion, mens numrene er de samme overalt. Målt: F2
# accepterer "00.07.03" og svarer med "00.07.03 - Aktindsigt -
# forvaltningsprincipper".
JOURNAL_PLAN = "00.07.03"          # Aktindsigt - forvaltningsprincipper
PROCESS_INSTRUCTION = "A53"        # Aktindsigtsanmodning mv.

# Aktindsigtssager holdes af postlisten. "Closed" heder "Undtage fra postliste" i
# AAK's opsætning - en anmodning indeholder ofte hvem der spørger om hvad.
EXTERNAL_ACCESS = "Closed"

# Akttyper. Passer én-til-én på KontAKTs beskedretninger, og det er ikke et
# tilfælde - F2 tænker om akter som post.
INBOUND, OUTBOUND, INTERNAL = "Inbound", "Outbound", "Internal"

# Mærket på et dokument, der er trukket ud af udleveringen. Et journaliseret
# dokument kan ikke fjernes i F2 - det HAR ingen slette-relation - så det bliver
# mærket i stedet. Præfiks og ikke suffiks: en sagsbehandler, der kigger på
# aktens dokumentliste, skal kunne se det uden at læse hele titlen.
SUPERSEDED = "UDGÅET - "

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
XML = {"Accept": "application/xml"}
PATCH_TYPE = {"Content-Type": "application/json-patch", "Accept": "application/xml"}


class F2Error(RuntimeError):
    """Et kald F2 afviste. Bærer serverens egen besked, ikke bare en statuskode."""

    def __init__(self, method: str, url: str, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(f"F2 {method} {url} -> {status}: {message}")


def party(*, email: str = "", login: str = "", party_no: Optional[int] = None,
          name: str = "") -> dict:
    """En PartyReference, som F2 faktisk kan opløse.

    Rækkefølgen er den mest robuste først: partynummeret er entydigt,
    mailadressen virker for både medarbejdere og borgere, og et login virker for
    tekniske brugere. ``name`` alene er IKKE en reference - det giver
    ``Could not find responsible party`` - men det må gerne følge en mail, og
    så bliver det navnet på en ekstern part, F2 opretter undervejs.

    ``PickFirstMatch`` hører til partynummeret: numre kan gå igen på tværs af
    organisationer, og uden flaget fejler opslaget som tvetydigt i stedet for at
    vælge.
    """
    if party_no is not None:
        return {"PartyNo": int(party_no), "PickFirstMatch": True}
    if email:
        return {"Name": name, "EMail": email} if name else {"EMail": email}
    if login:
        return {"Login": login}
    raise ValueError("en PartyReference kræver party_no, email eller login - "
                     "et navn alene kan F2 ikke opløse")


@dataclass
class Config:
    host: str                  # uden skema; https sættes på
    client_id: str
    client_secret: str
    f2_user: str = ""          # tom = samme navn som klienten
    env: str = "test"

    @property
    def base(self) -> str:
        h = (self.host or "").strip().strip("/")
        return h if h.startswith("http") else f"https://{h}"

    @property
    def user(self) -> str:
        return self.f2_user or self.client_id

    @classmethod
    def from_orchestrator(cls, oc, env: str = "") -> "Config":
        """Konfigurationen fra OpenOrchestrator.

        Der er kun to miljøafhængige værdier - værten og login - og de er samlet
        her, så et skifte til produktion er én constant:

            F2Miljoe          "test" eller "prod"   (mangler den: test)
            F2RestTestURL     værten, uden https://
            F2RestProdURL     ditto
            F2TESTRestAkt     credential: klient-id + hemmelighed
            F2PRODRestAkt     ditto
            F2BrugerTest      valgfri: F2-brugerens navn, hvis den ikke er
            F2BrugerProd      den samme som klientens

        Journalplan og handlingsfacet står IKKE her. De er KLE-numre og derfor
        de samme i test og drift - se konstanterne øverst. Afdelingerne står i
        KontAKTs egen database, fordi de skal kunne vælges i brugerfladen.
        """
        def constant(navn: str, standard: str = "") -> str:
            try:
                return (oc.get_constant(navn).value or "").strip()
            except Exception:
                return standard

        miljoe = (env or constant("F2Miljoe", "test") or "test").lower()
        if miljoe not in ("test", "prod"):
            raise ValueError(
                f"F2Miljoe er {miljoe!r} - den skal være 'test' eller 'prod'. "
                f"En stavefejl her ville journalisere i det forkerte miljø, så "
                f"den fejler med vilje frem for at gætte.")
        maerke = "Test" if miljoe == "test" else "Prod"
        cred = oc.get_credential(f"F2{maerke.upper()}RestAkt")
        return cls(host=constant(f"F2Rest{maerke}URL"),
                   client_id=cred.username,
                   client_secret=cred.password,
                   f2_user=constant(f"F2Bruger{maerke}"),
                   env=miljoe)


class F2:
    """Én forbindelse til F2REST. Genbrug den: token og service index caches."""

    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self.log = log
        self.session = requests.Session()
        self._token = ""
        self._expires = 0.0
        self._index: Optional[dict] = None

    # -- opdagelse ---------------------------------------------------------
    @property
    def index(self) -> dict:
        if self._index is None:
            r = self.session.get(self.cfg.base + SERVICE_INDEX,
                                 headers={"Accept": "application/json"},
                                 timeout=60)
            if not r.ok:
                raise F2Error("GET", SERVICE_INDEX, r.status_code, r.text[:300])
            self._index = r.json()
        return self._index

    def rel(self, name: str) -> str:
        """URL'en for en link-relation i service index."""
        try:
            return self.index[REL + name]["href"]
        except KeyError:
            haves = sorted(k.replace(REL, "") for k in self.index)
            raise F2Error("GET", SERVICE_INDEX, 404,
                          f"service index har ingen relation {name!r}. "
                          f"Den har: {', '.join(haves)}") from None

    # -- login -------------------------------------------------------------
    def token(self) -> str:
        """Bearer-tokenet, fornyet lidt før det udløber.

        To minutters margen: et token, der udløber midt i en journalisering,
        koster en halvt journaliseret sag.
        """
        if self._token and time.time() < self._expires - 120:
            return self._token
        r = self.session.post(
            self.rel("oauth2-token"),
            auth=HTTPBasicAuth(self.cfg.client_id, self.cfg.client_secret),
            data={"grant_type": "password", "username": self.cfg.user},
            headers={"Accept": "application/json"}, timeout=60)
        if not r.ok or "access_token" not in r.text:
            # Serverens egen besked er den brugbare her: "User not enabled for
            # this client" betyder at username mangler eller er den forkerte.
            raise F2Error("POST", "oauth2/token", r.status_code, r.text[:300])
        svar = r.json()
        self._token = svar["access_token"]
        self._expires = time.time() + int(svar.get("expires_in", 3600))
        return self._token

    # -- kald --------------------------------------------------------------
    def call(self, method: str, url: str, *, headers: Optional[dict] = None,
             expect: Iterable[int] = (200, 201, 204, 303), **kw):
        h = {"Authorization": f"Bearer {self.token()}", **XML, **(headers or {})}
        r = self.session.request(method, url, headers=h, timeout=300,
                                 allow_redirects=False, **kw)
        if r.status_code not in expect:
            raise F2Error(method, url, r.status_code, self._message(r))
        return r

    @staticmethod
    def _message(r) -> str:
        """Serverens fejlbesked, hvis den har en. F2 lægger den i <Message>."""
        try:
            return (ET.fromstring(r.content).findtext("cb:Message", "", NS)
                    or "").strip()[:300] or r.text[:200]
        except Exception:
            return r.text[:200].replace("\n", " ")

    # -- oprettelse (PEO) --------------------------------------------------
    def poe(self, factory_url: str, payload: Optional[dict] = None,
            files: Optional[dict] = None):
        """Opret noget i to trin, sådan som F2 kræver det.

        Trin 1 beder om en engangs-URL. Trin 2 POSTer data til den, og KAN
        gentages: 201 første gang, 303 hver gang derefter, og begge gange peger
        ``Location`` på forekomsten. Det er hele grunden til, at en robot kan
        køre det samme køelement igen uden at oprette dubletter.

        Returnerer (element, blev_oprettet_nu). Var svaret 303, har en tidligere
        kørsel allerede lavet den, og elementet hentes fra ``Location``.
        """
        r = self.call("POST", factory_url, headers=FORM, data="",
                      expect=(201, 303))
        engangs = r.headers.get("Location")
        if not engangs:
            raise F2Error("POST", factory_url, r.status_code,
                          "PEO-trin 1 svarede uden Location-header")

        if files is not None:
            r = self.call("POST", engangs, data=(payload or {}), files=files,
                          expect=(201, 303))
        else:
            r = self.call("POST", engangs, json=(payload or {}),
                          expect=(201, 303))
        if r.status_code == 303:
            sted = r.headers.get("Location")
            if not sted:
                raise F2Error("POST", engangs, 303, "303 uden Location")
            self.log(f"Fandtes allerede: {sted}")
            return self.get(sted), False
        return ET.fromstring(r.content), True

    def get(self, url: str):
        return ET.fromstring(self.call("GET", url).content)

    def patch(self, url: str, ops: list):
        """PATCH med et JSON-patch-dokument. F2 bruger kun ``replace``.

        Lister af parter kan ikke ændres element for element - hele listen skal
        udskiftes. Det gælder ``InvolvedParties``, ``Receivers`` og
        ``SupplementaryCaseWorkers``.
        """
        import json
        return self.call("PATCH", url, headers=PATCH_TYPE,
                         data=json.dumps(ops), expect=(200, 204))

    # -- sagen -------------------------------------------------------------
    def find_case(self, external_id: str) -> Optional[dict]:
        """Den F2-sag, der bærer dette ExternalId - eller None.

        Det er idempotensnøglen: KontAKT skriver ``KontAKT-35`` i ``ExternalId``,
        og så kan "har vi allerede journaliseret sag 35?" besvares med ét GET.

        MEN søgeindekset er forsinket med minutter. Lige efter en oprettelse
        giver den None, selv om sagen findes. Den er derfor en REPARATIONSVEJ -
        find sagen igen, hvis KontAKT har mistet nummeret - og ikke et opslag man
        laver før hver journalisering. Gem id og nummer ved oprettelsen i stedet.
        """
        if not (external_id or "").strip():
            return None
        r = self.call("GET", self.rel("case-search-filter"),
                      headers={"Accept": "text/csv"},
                      params={"externalId": external_id}, expect=(200,))
        raekker = list(csv.DictReader(io.StringIO(r.text)))
        return raekker[0] if raekker else None

    def case_by_number(self, case_number: str):
        """Sagen med dette sagsnummer. Går IKKE gennem søgeindekset, så den
        virker med det samme - og den normaliserer nummeret: både "2026 - 549"
        og "2026-549" rammer.

        Opslaget svarer **303** med sagens rigtige URL i ``Location`` og skal
        følges. Vi kan ikke bare lade requests følge den: hele klienten kører med
        ``allow_redirects=False``, fordi PEO'ens 303 er et SVAR og ikke en
        viderestilling - der skal vi selv læse ``Location``. Så den følges her,
        hvor vi ved hvad et 303 betyder.
        """
        skabelon = self.rel("case-by-case-number")
        url = skabelon.replace("{caseNumber}",
                               requests.utils.quote(case_number or ""))
        # 403 taeller med som "ikke fundet": F2 svarer det paa noget, der ikke
        # findes, praecis som paa noget man ikke maa se - et adgangsstyret
        # system skal ikke afsloere, at noget FINDES, som man ikke har adgang
        # til. De to kan altsaa ikke skelnes, og begge betyder "ingen sag her".
        r = self.call("GET", url, expect=(200, 303, 403, 404))
        if r.status_code == 303:
            sted = r.headers.get("Location")
            return self.get(sted) if sted else None
        return ET.fromstring(r.content) if r.status_code == 200 else None

    def create_case(self, *, title: str, external_id: str, responsible: dict,
                    deadline: str = "", cpr: str = "",
                    supplementary: Optional[list] = None):
        """Opret aktindsigtssagen, og sæt KontAKT-nummeret på.

        ``responsible`` er teamets F2-afdeling, ikke sagsbehandleren. Det er et
        valg, og grunden er ferier: "Ansvarlig enhed" i F2 er UDLEDT af den
        ansvarliges egen organisation og kan ikke sættes selvstændigt. Sætter man
        sagsbehandleren, flytter sagen afdeling, når en vikar tager over. Med
        afdelingen som ansvarlig står den stille, og menneskene kommer til via
        akternes ``InvolvedParties``.

        ``supplementary`` er rent oplysende - det er feltet "Suppl. sagsbeh." en
        F2-bruger kigger i for at se hvem der sidder på sagen. Det giver INGEN
        adgang; det er målt.

        Returnerer (sagselement, blev_oprettet_nu).
        """
        krop = {
            "Title": (title or "")[:255],
            "JournalPlanCode": JOURNAL_PLAN,
            "ProcessInstructionCode": PROCESS_INSTRUCTION,
            "ExternalAccessCode": EXTERNAL_ACCESS,
            "Responsible": responsible,
        }
        if supplementary:
            krop["SupplementaryCaseWorkers"] = list(supplementary)
        if deadline:
            krop["Deadline"] = deadline
        if cpr:
            krop["CprNumber"] = cpr

        sag, ny = self.poe(self.rel("create-case"), krop)
        if ny and external_id:
            # ExternalId kan kun sættes med PATCH - det står ikke i
            # oprettelsens felttabel. Gør vi det ikke, kan sagen ikke findes
            # igen, hvis KontAKT mister nummeret.
            self.patch(links(sag)["self"],
                       [{"op": "replace", "path": "/ExternalId",
                         "value": external_id[:255]}])
            sag = self.get(links(sag)["self"])
        return sag, ny

    def update_case(self, case, *, title: str = "", deadline: str = "",
                    responsible: Optional[dict] = None,
                    supplementary: Optional[list] = None):
        """Ret det, KontAKT er herre over, på en sag der allerede findes.

        Kun de felter, der faktisk er givet, sendes - en tom titel skal ikke
        kunne tømme sagens titel i F2, fordi kalderen ikke havde den med.

        ``responsible`` sættes, når sagen skifter team i KontAKT. Så flytter
        "Ansvarlig enhed" med, fordi den er udledt af den ansvarliges egen
        organisation og ikke kan sættes selvstændigt.
        """
        ops = []
        if title:
            ops.append({"op": "replace", "path": "/Title", "value": title[:255]})
        if deadline:
            ops.append({"op": "replace", "path": "/Deadline", "value": deadline})
        if responsible:
            ops.append({"op": "replace", "path": "/Responsible", "value": responsible})
        if supplementary is not None:
            ops.append({"op": "replace", "path": "/SupplementaryCaseWorkers",
                        "value": list(supplementary)})
        if not ops:
            return case
        url = links(case)["self"]
        self.patch(url, ops)
        return self.get(url)

    def structure(self, case):
        """Hele sagen - akter og dokumenter - i ét GET.

        Det er dokumentlisten. Pr. dokument kommer Id, Title, FileType,
        ContentType og Size. Bemærk at listen er filtreret efter hvad den
        aktuelle bruger må se ("all accessible matters"), så robottens billede
        er ikke nødvendigvis det samme som et menneskes.
        """
        return self.get(links(case)[REL + "structure"])

    # -- akten -------------------------------------------------------------
    def create_matter(self, case, *, kind: str, title: str, text: str = "",
                      responsible: dict, involved: Optional[list] = None,
                      sender: Optional[dict] = None,
                      receivers: Optional[list] = None,
                      received: str = "", sent: str = "",
                      archived: Optional[bool] = None):
        """En akt på sagen. ``kind`` er INBOUND, OUTBOUND eller INTERNAL.

        ``responsible`` SKAL være en enhed. Det er ikke en anbefaling: uden den
        mister robotten skriveretten til akten, og så kan afstemningen aldrig
        rette den igen. Det verificeres efter oprettelsen - se
        ``_kraev_enhed_som_ansvarlig``.

        ``involved`` er dem, der skal kunne finde sagen ud over afdelingen -
        sagsbehandleren og en eventuel vikar. Aktparter lægger sig OVEN PÅ
        enhedsadgangen i stedet for at erstatte den, og det er målt: en akt på
        ``Unit`` med Byggeri som ansvarlig var synlig for en person fra en anden
        afdeling, netop fordi han stod som aktpart.

        ``archived`` er kun et ønske. ``Inbound`` kommer ALTID tilbage
        arkiveret, uanset hvad man sender - modtaget post er en akt i det
        øjeblik den ankommer. Arkivering låser i øvrigt IKKE akten (målt: fem
        arkiverede akter havde ``DocumentsLocked=false``), så en arkiveret akt
        kan stadig få flere dokumenter - men den får til gengæld sit
        ``CaseMatterNumber``, som er aktnummeret et menneske ser.

        ``sent`` sættes med vilje IKKE på en udleveringsakt, der skal kunne
        vokse: en ``Outbound``-akt oprettet med ``SentDate`` kom tilbage
        ``DocumentsLocked=true``, og låsen kan ikke åbnes igen.

        Sagen behøver ikke nævnes i bodyen: den ligger i URL'en, fordi
        ``create-matter`` følges fra selve sagen.
        """
        if kind not in (INBOUND, OUTBOUND, INTERNAL):
            raise ValueError(f"ukendt akttype {kind!r}")
        krop = {
            "Type": kind,
            "Title": (title or "")[:255],
            "AccessLevel": ACCESS_LEVEL,
            "Responsible": responsible,
        }
        if text:
            krop["Text"] = text
        if sender:
            krop["Sender"] = sender
        if receivers:
            krop["Receivers"] = list(receivers)
        if received:
            krop["ReceivedDate"] = received
        if sent:
            krop["SentDate"] = sent
        if archived is not None:
            krop["Archived"] = bool(archived)

        # InvolvedParties staar MED VILJE ikke i kroppen. Se set_involved.
        akt, ny = self.poe(links(case)[REL + "create-matter"], krop)
        if involved:
            akt = self.set_involved(akt, involved)
        if ny:
            self._kraev_enhed_som_ansvarlig(akt)
        return akt, ny

    @staticmethod
    def _kraev_enhed_som_ansvarlig(matter) -> None:
        """Sig fra, hvis akten fik en PERSON som ansvarlig.

        Det er den anden halvdel af skriveretten (se modulets docstring):
        ``AccessLevel=Unit`` alene er ikke nok - med en person som ansvarlig
        lykkedes PATCH kun 2 af 4 gange, mens en enhed lykkedes 15 af 15.

        Der siges fra HER og ikke senere, fordi konsekvensen ellers er
        permanent: afstemningen ville få 403 på den akt ved hver eneste kørsel
        fra nu af, og en akt vi ikke kan røre, er tabt for altid. En robot, hvis
        køelement fejler ved oprettelsen, bliver set på med det samme.

        Kaldes kun for en akt, vi selv lige har oprettet - en akt, et menneske
        har lavet, er ikke vores at have en mening om.
        """
        ansvarlig = matter.find("cb:Responsible", NS)
        art = text(ansvarlig, "Type") if ansvarlig is not None else ""
        if art != "Unit":
            navn = text(ansvarlig, "Name") if ansvarlig is not None else "(ingen)"
            raise F2Error("POST", links(matter).get("self", "?"), 200,
                          f"akten fik {navn!r} ({art or 'ingen type'}) som "
                          f"ansvarlig, og ikke en enhed. Så mister robotten "
                          f"skriveretten til akten, og afstemningen kan aldrig "
                          f"rette den igen. Sæt afdelingen som ansvarlig, og "
                          f"læg sagsbehandleren i InvolvedParties i stedet.")

    def set_involved(self, matter, involved: list):
        """Sæt aktens parter, og kontrollér at de satte sig.

        HVORFOR DET IKKE SKER VED OPRETTELSEN: ``InvolvedParties`` står i
        dokumentationens felttabel for at oprette en akt, men F2 **ignorerer det
        stiltiende**. Målt 2026-09-07: tre akter oprettet med
        ``InvolvedParties`` i kroppen svarede alle 201 Created og kom tilbage
        uden en eneste aktpart, mens ``Receivers`` i samme kald sad fint fast.
        Sat med PATCH bagefter sidder de.

        Det er den værste slags fejl her: adgangen hænger på netop det felt, så
        en sag ville blive journaliseret "korrekt" og være usynlig for
        sagsbehandleren. Derfor læses akten tilbage, og der siges fra, hvis
        parterne ikke er der. En robot, hvis køelement fejler, bliver set på; en
        usynlig sag bliver det ikke.
        """
        url = links(matter)["self"]
        self.patch(url, [{"op": "replace", "path": "/InvolvedParties",
                          "value": list(involved)}])
        akt = self.get(url)
        if not akt.findall("cb:InvolvedParties/cb:InvolvedParty", NS):
            raise F2Error("PATCH", url, 200,
                          "aktparterne satte sig ikke. Uden dem kan hverken "
                          "sagsbehandleren eller en vikar finde sagen, så der "
                          "siges fra frem for at journalisere noget usynligt.")
        return akt

    def add_document(self, matter, *, title: str, filename: str, data: bytes,
                     content_type: str = "", description: str = ""):
        """En fil på akten.

        ``filename`` skal have den rigtige endelse: F2 udleder filtypen af den,
        ikke af content-typen. ``title`` er det, mennesker ser, og bør være uden
        endelse - cBrain anbefaler det selv, og titlen må gerne bære æøå, hvilket
        et filnavn i en multipart-header ikke må.
        """
        felter = {"Title": (title or filename)[:255]}
        if description:
            felter["Description"] = description
        return self.poe(links(matter)[REL + "create-document"], felter,
                        files={"File": (filename, data,
                                        content_type or "application/octet-stream")})

    def content(self, document) -> bytes:
        """Dokumentets bytes. Strukturens dokumenter bærer ikke selv
        indholdslinket, så det hentes fra dokumentets egen repræsentation."""
        return self._fetch(document, "content")

    def pdf_content(self, document, *, flatten: bool = False) -> bytes:
        """Dokumentet som PDF - konverteret af F2, ikke af os.

        Målt: en 52-byte ``text/plain`` kom tilbage som en rigtig PDF. Det er
        grunden til, at ingen KontAKT-robot behøver LibreOffice for at få et
        F2-dokument som PDF; F2 gør det selv, og resultatet er det samme, uanset
        hvilken robotmaskine der spørger.

        ``flatten`` beder om ``pdf-content-flattened``. Til en udlevering er det
        den rigtige: en fladet PDF har ikke lag eller annotationer, tekst kan
        gemme sig under. Bekræft dog med cBrain hvad "flattened" dækker, før den
        bruges som eneste sikring ved en overstregning.
        """
        return self._fetch(document,
                           "pdf-content-flattened" if flatten else "pdf-content")

    def text_content(self, document) -> bytes:
        """Dokumentets tekst, som F2 har trukket den ud. Tom for et billede."""
        return self._fetch(document, "text-content")

    def _fetch(self, document, rel_navn: str) -> bytes:
        """Hent en af dokumentets indholdsformer.

        Strukturens dokumenter bærer kun ``self`` og ``alternate``, så
        indholdslinkene hentes fra dokumentets egen repræsentation, hvis de ikke
        er der. Det koster et GET og sparer en fejl, der er svær at forstå.
        """
        L = links(document)
        url = L.get(REL + rel_navn)
        if not url:
            document = self.get(L["self"])
            L = links(document)
            url = L.get(REL + rel_navn)
        if not url:
            raise F2Error("GET", L.get("self", "?"), 404,
                          f"dokumentet har ingen {rel_navn}-relation. Det har: "
                          + ", ".join(sorted(r.replace(REL, "") for r in L)))
        return self.call("GET", url, headers={"Accept": "*/*"}).content

    # -- rette noget, der allerede ligger der ------------------------------
    def set_title(self, node, title: str):
        """Ret titlen på en akt eller et dokument, og læs den tilbage.

        Målt at virke på begge - og verificeret, fordi F2 har vist os tre gange,
        at et 200 ikke er et bevis på, at feltet satte sig (se
        ``set_involved``). Siger fra, hvis titlen ikke blev den, vi bad om.
        """
        url = links(node)["self"]
        ny = (title or "")[:255]
        self.patch(url, [{"op": "replace", "path": "/Title", "value": ny}])
        opdateret = self.get(url)
        if text(opdateret, "Title") != ny:
            raise F2Error("PATCH", url, 200,
                          f"titlen satte sig ikke - den er stadig "
                          f"{text(opdateret, 'Title')!r}")
        return opdateret

    def mark_superseded(self, document, praefiks: str = SUPERSEDED):
        """Mærk et dokument som trukket ud af udleveringen.

        Et journaliseret dokument kan ikke fjernes - det har ingen
        slette-relation. Så når en sagsbehandler trækker et dokument ud, eller
        en ny udgave erstatter det, bliver det gamle mærket i stedet for at
        forsvinde. F2-sagen fortæller så historien i stedet for at blive skrevet
        om, og aktlisten, der journaliseres ved samme kørsel, er det
        autoritative indeks over hvad der faktisk blev udleveret.

        Idempotent: et dokument, der allerede er mærket, bliver ikke mærket to
        gange. Robotten kan køre samme køelement igen.
        """
        titel = text(document, "Title")
        if titel.startswith(praefiks):
            return document
        return self.set_title(document, f"{praefiks}{titel}")

    @staticmethod
    def locked(matter) -> bool:
        """Er aktens dokumenter låst? Så kan der ikke lægges flere på.

        Spørges FØR et dokument lægges op, fordi svaret ellers er et 400, der
        ligner en rettighedsfejl (``Du har ikke rettigheder til at oprette
        dokumentet``) og ikke det den er: akten er lukket for nyt indhold. Og
        den kan ikke låses op - ``set-documents-locked`` svarer 400/500 - så den
        rigtige reaktion er at lægge en NY akt på.
        """
        return text(matter, "DocumentsLocked").lower() == "true"

    @staticmethod
    def matter_number(matter) -> Optional[int]:
        """Aktnummeret i sagen (1, 2, 3 …), eller None hvis akten ikke er
        arkiveret endnu - så har den ingen i F2 heller."""
        raw = text(matter, "CaseMatterNumber")
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    # -- sletning ----------------------------------------------------------
    def delete_case(self, case, reason: str) -> bool:
        """Slet sagen, og sig sandheden om hvorvidt det lykkedes.

        F2 svarer **204 uden at gøre noget**, hvis kontoen ikke har privilegiet -
        ikke 403. Kun dry-run melder ærligt. Derfor: prøv dry-run først, og
        verificér med et GET bagefter. Et 204 er ikke et bevis på noget.
        """
        L = links(case)
        selv = L["self"]
        proeve = L.get(REL + "delete-case-dry-run")
        if proeve:
            r = self.session.post(
                proeve, data={"DeleteReason": reason},
                headers={"Authorization": f"Bearer {self.token()}", **FORM, **XML},
                timeout=300)
            if not r.ok:
                self.log(f"Sletning nægtet ved dry-run: {self._message(r)}")
                return False
        self.call("POST", L[REL + "delete-case"], headers=FORM,
                  data={"DeleteReason": reason,
                        "GenerateReportForDeletedItems": "false"},
                  expect=(200, 204))
        r = self.session.get(selv, headers={"Authorization": f"Bearer {self.token()}",
                                            **XML}, timeout=60)
        vaek = r.status_code >= 400
        if not vaek:
            self.log(f"F2 svarede at sagen blev slettet, men den kan stadig "
                     f"hentes ({r.status_code}) - den er IKKE slettet")
        return vaek


# ---------------------------------------------------------------------------
# Læsehjælp til F2's XML
# ---------------------------------------------------------------------------


def links(node) -> dict:
    """Elementets links som ``rel -> href``.

    Hele navigationen i F2 går gennem dem, så det her er den mest brugte
    funktion i filen.
    """
    return {(l.get("rel") or ""): (l.get("href") or "")
            for l in node.findall("cb:Link", NS)}


def text(node, felt: str, standard: str = "") -> str:
    return (node.findtext(f"cb:{felt}", standard, NS) or standard).strip()


def matters(structure) -> list:
    """Akterne i en sagsstruktur."""
    return structure.findall("cb:Matters/cb:Matter", NS)


def documents(matter) -> list:
    """Dokumenterne på en akt i en sagsstruktur."""
    return matter.findall("cb:Documents/cb:Document", NS)


def parties(collection) -> list:
    """Parterne i en partssamling, som fx en sags ``rel/parties``.

    Elementnavnet skifter med partens TYPE - ``<User>``, ``<Unit>``, ``<Party>``
    - og er ikke det samme på alle party-endpoints. Derfor kigges der på
    indholdet: alt med et navn tæller som en part.
    """
    ud = []
    for x in collection.iter():
        if x.tag.split("}")[-1] in ("Parties", "Items", "Link"):
            continue
        if text(x, "Name"):
            ud.append(x)
    return ud
