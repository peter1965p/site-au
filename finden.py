#!/usr/bin/env python3
"""Firmen mit Website in einer Region finden - Rohstoff fuer audit.py.

Quelle ist OpenStreetMap ueber die Overpass-API: offene Daten unter ODbL,
anders als bei Google Maps ohne Nutzungsbeschraenkung fuer genau diesen Zweck.
Geliefert werden nur Betriebe, bei denen eine Website hinterlegt ist - ohne
Website gibt es nichts zu pruefen.

Aufruf:  python finden.py --region vulkaneifel --branchen handwerk,gastronomie
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
from rich.console import Console
from rich.table import Table

console = Console()

USER_AGENT = "site-audit/1.0 (+paeffgen-it.de)"

# Overpass ist ein gespendeter Dienst mit engen Grenzen. Mehrere Spiegel,
# grosszuegige Wartezeiten und nur eine Abfrage auf einmal.
# Nur Spiegel, die auch wirklich antworten. private.coffee und osm.ch waren
# beim Test dauerhaft defekt (HTTP 500 bzw. 200 ohne JSON) - ein kaputter
# Spiegel ist schlimmer als keiner, weil eine unbrauchbare Antwort wie
# "keine Treffer" aussieht.
ENDPUNKTE = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
VERSUCHE = 3
WARTEN = 25.0  # Sekunden nach einer Abfuhr, waechst mit jedem Versuch

SLOT_RE = re.compile(r"Slot available after: .*?in (\d+) seconds", re.IGNORECASE)
FREI_RE = re.compile(r"(\d+) slots? available now", re.IGNORECASE)


def wartezeit() -> int:
    """Wie lange der Hauptserver noch braucht, bis wieder eine Abfrage darf.

    Overpass begrenzt pro IP auf zwei gleichzeitige Abfragen und meldet im
    Status, wann der naechste Platz frei wird. Das abzufragen ist besser,
    als blind nachzulegen - jeder Versuch zur Unzeit verlaengert die Sperre.
    Rueckgabe in Sekunden, 0 wenn sofort moeglich.
    """
    try:
        antwort = httpx.get(
            "https://overpass-api.de/api/status", timeout=20.0, headers={"User-Agent": USER_AGENT}
        )
        if antwort.status_code != 200:
            return 0
        text = antwort.text
        frei = FREI_RE.search(text)
        if frei and int(frei.group(1)) > 0:
            return 0
        wartezeiten = [int(m) for m in SLOT_RE.findall(text)]
        return min(wartezeiten) if wartezeiten else 0
    except Exception:  # noqa: BLE001 - Status ist nur eine Hilfe, kein Muss
        return 0


@dataclass(frozen=True)
class Region:
    titel: str
    filter: str  # vollstaendiger Overpass-Gebietsausdruck, z. B. area[...] oder area(id)


# Die 16 Bundeslaender ueber ihre ISO-Codes. admin_level=4 ist noetig, weil
# sonst auch Regierungsbezirke und Stadtstaaten doppelt greifen.
BUNDESLAENDER: dict[str, str] = {
    "DE-BW": "Baden-Württemberg",
    "DE-BY": "Bayern",
    "DE-BE": "Berlin",
    "DE-BB": "Brandenburg",
    "DE-HB": "Bremen",
    "DE-HH": "Hamburg",
    "DE-HE": "Hessen",
    "DE-MV": "Mecklenburg-Vorpommern",
    "DE-NI": "Niedersachsen",
    "DE-NW": "Nordrhein-Westfalen",
    "DE-RP": "Rheinland-Pfalz",
    "DE-SL": "Saarland",
    "DE-SN": "Sachsen",
    "DE-ST": "Sachsen-Anhalt",
    "DE-SH": "Schleswig-Holstein",
    "DE-TH": "Thüringen",
}

# Erste zwei Ziffern des Regionalschluessels je Bundesland. Darueber lassen
# sich die Kreise eines Landes zuverlaessig greifen - die naheliegende
# Abfrage "alle Relationen in der Flaeche" liefert auch angrenzende Kreise
# der Nachbarlaender mit.
RS_PRAEFIX: dict[str, str] = {
    "DE-SH": "01", "DE-HH": "02", "DE-NI": "03", "DE-HB": "04",
    "DE-NW": "05", "DE-HE": "06", "DE-RP": "07", "DE-BW": "08",
    "DE-BY": "09", "DE-SL": "10", "DE-BE": "11", "DE-BB": "12",
    "DE-MV": "13", "DE-SN": "14", "DE-ST": "15", "DE-TH": "16",
}


def land_filter(iso: str) -> str:
    return f'area["ISO3166-2"="{iso}"]["admin_level"="4"]'


def kreis_filter(schluessel: str) -> str:
    """Overpass-Gebiet eines Kreises ueber seinen Kreisschluessel (5 Stellen).

    Greift auf zwei Tags zu, weil OpenStreetMap nicht jeden Kreis mit dem
    Regionalschluessel fuehrt: Landkreis Greiz (16076) traegt nur den amtlichen
    Gemeindeschluessel. Ohne den zweiten Zweig liefert eine Suche dort nichts -
    stumm und ohne Fehlermeldung. Das Praefix trifft zugleich die Gemeinden im
    Kreis, die ohnehin innerhalb seiner Flaeche liegen.
    """
    return (
        '(area["de:regionalschluessel"~"^%s"];'
        'area["de:amtlicher_gemeindeschluessel"~"^%s"];)' % (schluessel, schluessel)
    )


# Was OpenStreetMap als Kreis fuehrt, aber keiner ist.
KEIN_KREIS = {
    "06415": "Hanau - Stadt mit Sonderstatus im Main-Kinzig-Kreis. Der Schluessel stammt aus "
             "der Zeit, als sie kreisfrei war.",
}


def kreis_schluessel(tags: dict) -> str:
    """Fuenfstelliger Kreisschluessel aus Regionalschluessel oder amtlichem Gemeindeschluessel."""
    wert = (tags.get("de:regionalschluessel") or tags.get("de:amtlicher_gemeindeschluessel") or "").strip()
    return wert[:5] if len(wert) >= 5 and wert[:5].isdigit() else ""


# Die Kreislisten ruecken nur bei Gebietsreformen nach. Deshalb liegt eine
# mitgelieferte Fassung im Projekt (kreise_de.json), die ohne Netz auskommt.
# Der lokale Zwischenspeicher (.kreise.json) hat Vorrang, falls jemand ihn per
# Aktualisierung erneuert hat. Erst wenn beides fehlt, wird Overpass gefragt.
KREISE_DATEI = Path(__file__).resolve().parent / "kreise_de.json"
KREIS_CACHE = Path(__file__).resolve().parent / ".kreise.json"


def _lies_kreise(pfad: Path, iso: str) -> list[tuple[str, str]] | None:
    import json

    try:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    eintrag = daten.get(iso)
    if not isinstance(eintrag, list) or not eintrag:
        return None
    ergebnis: list[tuple[str, str]] = []
    gesehen: set[str] = set()
    for n, r in eintrag:
        schluessel = str(r).strip()[:5]  # Kreisschluessel, auch aus aelteren 12-stelligen Eintraegen
        if schluessel in gesehen or schluessel in KEIN_KREIS:
            continue
        gesehen.add(schluessel)
        ergebnis.append((str(n), schluessel))
    return ergebnis or None


# Stadtstaaten ohne Kreisebene: das Land ist zugleich die Stadt.
KEINE_KREISE = frozenset({"DE-BE", "DE-HH"})


def kreise_lokal(iso: str) -> list[tuple[str, str]] | None:
    """Kreise eines Bundeslands ohne Netzzugriff, oder None.

    Fuer Berlin und Hamburg eine leere Liste - die gibt es dort nicht, und eine
    Abfrage im Netz wuerde nie etwas finden.
    """
    if iso in KEINE_KREISE:
        return []
    return _lies_kreise(KREIS_CACHE, iso) or _lies_kreise(KREISE_DATEI, iso)


def kreise(iso: str, erzwingen: bool = False, max_sekunden: float | None = 60.0) -> list[tuple[str, str]]:
    """Alle Kreise und kreisfreien Staedte eines Bundeslands.

    Rueckgabe: Liste aus (Name, Regionalschluessel), alphabetisch sortiert.
    Bei Berlin und Hamburg ist sie leer: dort gibt es keine Kreisebene, das
    Bundesland ist zugleich die Stadt.
    """
    import json

    if not erzwingen:
        lokal = kreise_lokal(iso)
        if lokal is not None:
            return lokal

    praefix = RS_PRAEFIX[iso]
    # Beide Schluessel-Tags, siehe kreis_filter(): manche Kreise tragen nur den
    # amtlichen Gemeindeschluessel und waeren sonst unsichtbar.
    query = (
        "[out:json][timeout:60];\n"
        "(\n"
        '  rel["de:regionalschluessel"~"^%s"]["admin_level"="6"]["boundary"="administrative"];\n'
        '  rel["de:amtlicher_gemeindeschluessel"~"^%s"]["admin_level"="6"]["boundary"="administrative"];\n'
        ");\n"
        "out tags;" % (praefix, praefix)
    )
    gefunden: list[tuple[str, str]] = []
    gesehen: set[str] = set()
    for el in abfragen(query, leise=True, max_sekunden=max_sekunden):
        tags = el.get("tags", {}) or {}
        name = (tags.get("name") or "").strip()
        schluessel = kreis_schluessel(tags)
        if not name or not schluessel or schluessel in gesehen or schluessel in KEIN_KREIS:
            continue
        gesehen.add(schluessel)
        gefunden.append((name, schluessel))
    gefunden.sort(key=lambda x: x[0].lower())

    try:
        bestand = json.loads(KREIS_CACHE.read_text(encoding="utf-8")) if KREIS_CACHE.exists() else {}
    except (OSError, ValueError):
        bestand = {}
    bestand[iso] = [[n, r] for n, r in gefunden]
    try:
        KREIS_CACHE.write_text(json.dumps(bestand, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return gefunden


def kreise_erneuern(ziel: Path | None = None, pause: float = 4.0, melde=None) -> dict:
    """Alle Kreislisten neu aus OpenStreetMap ziehen und als kreise_de.json ablegen.

    Noetig nur nach einer Gebietsreform (zuletzt etwa Zusammenlegungen von
    Landkreisen). Eine Abfrage je Bundesland mit Pause dazwischen: Overpass ist
    ein gespendeter Dienst, 16 Abfragen hintereinander ohne Luft waeren
    unhoeflich. Wird ein Land nicht geladen, bleibt dessen bisheriger Eintrag
    stehen - die Datei wird nie schlechter als sie war.
    """
    import datetime
    import json

    ziel = ziel or KREISE_DATEI
    try:
        daten = json.loads(ziel.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        daten = {}
    fehlgeschlagen: list[str] = []
    for iso in sorted(RS_PRAEFIX):
        if iso in KEINE_KREISE:
            daten[iso] = []
            continue
        try:
            daten[iso] = [[n, r] for n, r in kreise(iso, erzwingen=True, max_sekunden=180.0)]
        except Exception as exc:  # noqa: BLE001 - ein Land darf die uebrigen nicht verhindern
            fehlgeschlagen.append(iso)
            if melde:
                melde(f"{iso}: {str(exc).splitlines()[0]}")
            continue
        if melde:
            melde(f"{iso} {BUNDESLAENDER[iso]}: {len(daten[iso])} Kreise")
        time.sleep(pause)
    if not fehlgeschlagen:
        daten["_stand"] = datetime.date.today().isoformat()
    ziel.write_text(json.dumps(daten, ensure_ascii=False, indent=1), encoding="utf-8")
    daten["_fehlgeschlagen"] = fehlgeschlagen
    return daten


# Peters Einzugsgebiet als Schnellwahl. Die Regionalschluessel sind stabiler
# als Namen, die in OSM schon mal umbenannt oder anders geschrieben werden.
REGIONEN: dict[str, Region] = {
    "vulkaneifel": Region("Landkreis Vulkaneifel", kreis_filter("07233")),
    "bitburg-pruem": Region("Eifelkreis Bitburg-Prüm", kreis_filter("07232")),
    "bernkastel-wittlich": Region("Bernkastel-Wittlich", kreis_filter("07231")),
    "cochem-zell": Region("Cochem-Zell", kreis_filter("07135")),
    "ahrweiler": Region("Ahrweiler", kreis_filter("07131")),
    "trier": Region("Trier", kreis_filter("07211")),
    "trier-saarburg": Region("Trier-Saarburg", kreis_filter("07235")),
    "bremen": Region("Bremen", kreis_filter("04011")),
    "bremerhaven": Region("Bremerhaven", kreis_filter("04012")),
    "cuxhaven": Region("Landkreis Cuxhaven", kreis_filter("03352")),
    "wurster-nordseekueste": Region(
        "Wurster Nordseeküste", 'area["name"="Wurster Nordseeküste"]["boundary"="administrative"]'
    ),
}

# Gruppen von OSM-Tags, jeweils mit der Branche, die in der CSV landet.
BRANCHEN: dict[str, tuple[str, list[str]]] = {
    "beherbergung": (
        "Beherbergung",
        ['["tourism"~"^(hotel|guest_house|apartment|chalet|hostel|motel|camp_site)$"]'],
    ),
    "gastronomie": (
        "Gastronomie",
        ['["amenity"~"^(restaurant|cafe|pub|bar|biergarten|ice_cream)$"]'],
    ),
    "handwerk": ("Handwerk", ['["craft"]', '["shop"~"^(bakery|butcher|florist|car_repair)$"]']),
    "einzelhandel": ("Einzelhandel", ['["shop"]']),
    "dienstleistung": ("Dienstleistung", ['["office"]']),
    "gesundheit": (
        "Gesundheit",
        ['["amenity"~"^(doctors|dentist|pharmacy|veterinary)$"]', '["healthcare"]'],
    ),
    "freizeit": ("Freizeit", ['["leisure"]', '["tourism"~"^(museum|attraction)$"]']),
}


@dataclass
class Firma:
    name: str
    url: str
    ort: str = ""
    branche: str = ""
    telefon: str = ""
    email: str = ""
    quelle: str = ""  # OSM-Typ und Id, damit man nachschauen kann
    tags: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abfrage
# ---------------------------------------------------------------------------


def bauen(region: Region, branchen: list[str], nur_mit_website: bool = True) -> str:
    teile: list[str] = []
    for schluessel in branchen:
        _titel, selektoren = BRANCHEN[schluessel]
        for sel in selektoren:
            web = '["website"]' if nur_mit_website else ""
            teile.append(f"  nwr{web}{sel}(area.a);")
    koerper = "\n".join(teile)
    return (
        "[out:json][timeout:180];\n"
        f"{region.filter}->.a;\n"
        f"(\n{koerper}\n);\n"
        "out tags center;"
    )


def abfragen(query: str, leise: bool = False, max_sekunden: float | None = None) -> list[dict]:
    """Overpass fragen, mit Spiegeln und Geduld.

    Der Dienst antwortet unter Last mit 429 oder 504. Das ist kein Fehler,
    sondern eine Bitte zu warten - wer sofort nachlegt, wird haerter gedrosselt.
    Deshalb wird vorab der Status gelesen und die genannte Wartezeit
    abgesessen, statt blind zu versuchen.

    `max_sekunden` ist ein Zeitbudget fuer alles zusammen (Warten, Abfragen,
    Wiederholungen). Ohne Budget kann der Aufruf bei Ausfaellen weit ueber
    zwanzig Minuten dauern - fuer Dialoge, in denen jemand wartet, ist das
    untragbar. Ist das Budget aufgebraucht, gibt es einen Fehler statt Warten.
    """
    frist = None if max_sekunden is None else time.monotonic() + max_sekunden

    def uebrig() -> float | None:
        return None if frist is None else frist - time.monotonic()

    meldungen: dict[str, str] = {}

    def fehler(grund: str) -> RuntimeError:
        # Alle Server nennen, nicht nur den zuletzt befragten: oft ist der
        # Hauptserver ausgelastet und ein Spiegel tot, und nur die Meldung des
        # toten Spiegels zu zeigen fuehrt in die Irre.
        details = " · ".join(f"{h}: {m}" for h, m in meldungen.items()) or "keine Antwort"
        return RuntimeError(
            f"{grund}\n\nMeldungen: {details}\n\n"
            "Overpass/OpenStreetMap ist ein gespendeter Dienst mit engen Grenzen. "
            "In ein paar Minuten noch einmal versuchen, oder ein kleineres Gebiet waehlen."
        )

    rest = wartezeit()
    if rest > 0:
        if uebrig() is not None and rest + 1 > uebrig():
            meldungen["overpass-api.de"] = f"laesst erst in {rest} s wieder eine Abfrage zu"
            raise fehler("Die Firmendatenbank ist gerade ausgelastet.")
        if not leise:
            console.print(
                f"[yellow]Overpass laesst gerade keine weitere Abfrage zu. "
                f"Warte {rest} s …[/yellow]"
            )
        time.sleep(min(rest, 180) + 1)

    for versuch in range(1, VERSUCHE + 1):
        for endpunkt in ENDPUNKTE:
            host = urlparse(endpunkt).hostname or endpunkt
            if uebrig() is not None and uebrig() <= 2:
                raise fehler("Die Firmendatenbank antwortet nicht in der vorgesehenen Zeit.")
            try:
                antwort = httpx.post(
                    endpunkt,
                    data={"data": query},
                    timeout=240.0 if frist is None else max(5.0, min(240.0, uebrig())),
                    headers={"User-Agent": USER_AGENT},
                )
            except Exception as exc:  # noqa: BLE001
                meldungen[host] = type(exc).__name__
                continue
            if antwort.status_code == 200:
                try:
                    daten = antwort.json()
                except ValueError:
                    # Manche Spiegel liefern 200 mit einer HTML-Fehlerseite.
                    meldungen[host] = "Antwort ist kein JSON"
                    continue
                if "elements" not in daten:
                    meldungen[host] = "Antwort ohne Ergebnisfeld"
                    continue
                return daten["elements"]
            meldungen[host] = f"HTTP {antwort.status_code}"
        if versuch < VERSUCHE:
            pause = WARTEN * versuch
            if uebrig() is not None and pause >= uebrig():
                break
            if not leise:
                console.print(
                    f"[yellow]Overpass ist gerade ausgelastet "
                    f"({' · '.join(f'{h}: {m}' for h, m in meldungen.items())}). "
                    f"Warte {pause:.0f}s und versuche es erneut …[/yellow]"
                )
            time.sleep(pause)
    raise fehler("Die Firmendatenbank antwortet gerade nicht.")


MAIL_PREFIX = re.compile(r"^mailto:", re.IGNORECASE)


def aufbereiten(elemente: list[dict], branchen: list[str]) -> list[Firma]:
    """OSM-Elemente in Firmen umwandeln, Dubletten nach Domain zusammenfassen."""
    nach_domain: dict[str, Firma] = {}
    reihenfolge: list[str] = []

    for el in elemente:
        t = el.get("tags", {})
        url = (t.get("website") or t.get("contact:website") or "").strip()
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if not host or "." not in host:
            continue
        # Verzeichnis- und Plattformeintraege sind nicht die Seite der Firma
        if any(
            host.endswith(x)
            for x in ("facebook.com", "instagram.com", "booking.com", "airbnb.de", "airbnb.com",
                      "tripadvisor.de", "google.com", "linktr.ee", "wixsite.com", "jimdosite.com")
        ):
            continue

        name = (t.get("name") or t.get("operator") or "").strip()
        if not name:
            continue

        firma = Firma(
            name=name,
            url=host,
            ort=(t.get("addr:city") or t.get("addr:suburb") or "").strip(),
            branche=branche_von(t, branchen),
            telefon=(t.get("phone") or t.get("contact:phone") or "").strip(),
            email=MAIL_PREFIX.sub("", (t.get("email") or t.get("contact:email") or "").strip()),
            quelle=f"{el.get('type','?')}/{el.get('id','?')}",
            tags=t,
        )
        if host in nach_domain:
            # Mehrere Eintraege derselben Firma: fehlende Angaben ergaenzen
            vorhanden = nach_domain[host]
            vorhanden.telefon = vorhanden.telefon or firma.telefon
            vorhanden.email = vorhanden.email or firma.email
            vorhanden.ort = vorhanden.ort or firma.ort
            continue
        nach_domain[host] = firma
        reihenfolge.append(host)

    return [nach_domain[h] for h in reihenfolge]


def branche_von(tags: dict[str, str], gewaehlt: list[str]) -> str:
    for schluessel in gewaehlt:
        titel, selektoren = BRANCHEN[schluessel]
        for sel in selektoren:
            schluessel_name = sel.split('"')[1]
            if schluessel_name in tags:
                return titel
    for schluessel, (titel, _sel) in BRANCHEN.items():
        if schluessel in tags:
            return titel
    return ""


def suchen(region_name: str, branchen: list[str], leise: bool = False) -> list[Firma]:
    if region_name not in REGIONEN:
        raise SystemExit(
            f"Unbekannte Region {region_name!r}. Bekannt: {', '.join(sorted(REGIONEN))}"
        )
    return suchen_in(REGIONEN[region_name], branchen, leise=leise)


def suchen_in(region: Region, branchen: list[str], leise: bool = False) -> list[Firma]:
    """In einem beliebigen Gebiet suchen - Bundesland, Kreis oder Schnellwahl."""
    unbekannt = [b for b in branchen if b not in BRANCHEN]
    if unbekannt:
        raise SystemExit(
            f"Unbekannte Branche(n): {', '.join(unbekannt)}. Bekannt: {', '.join(BRANCHEN)}"
        )
    if not branchen:
        return []
    elemente = abfragen(bauen(region, branchen), leise=leise)
    return aufbereiten(elemente, branchen)


# ---------------------------------------------------------------------------
# Ausgabe
# ---------------------------------------------------------------------------


def schreiben(firmen: list[Firma], pfad: Path) -> None:
    with pfad.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "url", "ort", "branche", "telefon", "email", "quelle"])
        for f in firmen:
            w.writerow([f.name, f.url, f.ort, f.branche, f.telefon, f.email, f.quelle])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="finden.py",
        description="Firmen mit Website in einer Region aus OpenStreetMap holen.",
        epilog="Regionen: " + ", ".join(sorted(REGIONEN)) + "\nBranchen: " + ", ".join(BRANCHEN),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--region", help="z. B. vulkaneifel")
    p.add_argument(
        "--kreise-erneuern",
        action="store_true",
        help="Kreislisten aller Bundeslaender neu aus OpenStreetMap holen (selten noetig)",
    )
    p.add_argument(
        "--branchen",
        default="beherbergung,gastronomie,handwerk",
        help="kommagetrennt, Vorgabe: beherbergung,gastronomie,handwerk",
    )
    p.add_argument("--out", type=Path, default=Path("leads.csv"), help="Ziel-CSV")
    p.add_argument("--anhaengen", action="store_true", help="an eine vorhandene CSV anhaengen")
    args = p.parse_args(argv)

    if args.kreise_erneuern:
        erg = kreise_erneuern(melde=console.print)
        schlecht = erg.pop("_fehlgeschlagen")
        console.print(f"[green]Geschrieben:[/green] {KREISE_DATEI}")
        if schlecht:
            console.print(f"[yellow]Nicht geladen, alter Stand bleibt: {', '.join(schlecht)}[/yellow]")
        return 1 if schlecht else 0
    if not args.region:
        p.error("--region ist erforderlich (oder --kreise-erneuern)")

    branchen = [b.strip() for b in args.branchen.split(",") if b.strip()]
    region = REGIONEN.get(args.region)
    if region is None:
        raise SystemExit(f"Unbekannte Region {args.region!r}. Bekannt: {', '.join(sorted(REGIONEN))}")

    console.print(f"[bold]Suche in {region.titel}[/bold] — {', '.join(branchen)}")
    firmen = suchen(args.region, branchen)

    if args.anhaengen and args.out.exists():
        import audit

        vorhanden = {audit.url_key(l.url) for l in audit.read_leads(args.out)}
        neu = [f for f in firmen if audit.url_key(f.url) not in vorhanden]
        console.print(f"{len(firmen) - len(neu)} davon stehen schon in {args.out}")
        with args.out.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            for f in neu:
                w.writerow([f.name, f.url, f.ort, f.branche, f.telefon, f.email, f.quelle])
        firmen = neu
    else:
        schreiben(firmen, args.out)

    tabelle = Table(title=f"{len(firmen)} Firmen mit Website")
    tabelle.add_column("Firma", max_width=34)
    tabelle.add_column("Ort", max_width=16)
    tabelle.add_column("Branche", max_width=14)
    tabelle.add_column("Website", max_width=30)
    tabelle.add_column("Tel", width=3, justify="center")
    for f in firmen[:25]:
        tabelle.add_row(f.name, f.ort or "–", f.branche or "–", f.url, "✓" if f.telefon else "")
    console.print(tabelle)
    if len(firmen) > 25:
        console.print(f"[dim]… und {len(firmen) - 25} weitere[/dim]")
    console.print(f"\n[green]Geschrieben:[/green] {args.out}")
    console.print(f"Weiter mit: [bold]python audit.py {args.out} --out ergebnis[/bold]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
