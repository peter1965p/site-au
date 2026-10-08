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
ENDPUNKTE = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
VERSUCHE = 3
WARTEN = 20.0  # Sekunden nach einer Abfuhr, waechst mit jedem Versuch


@dataclass(frozen=True)
class Region:
    titel: str
    filter: str  # Overpass-Selektor fuer das Gebiet


# Peters Einzugsgebiet. Die Regionalschluessel sind stabiler als Namen, die
# in OSM schon mal umbenannt oder unterschiedlich geschrieben werden.
REGIONEN: dict[str, Region] = {
    "vulkaneifel": Region("Landkreis Vulkaneifel", '["de:regionalschluessel"="07233"]'),
    "bitburg-pruem": Region("Eifelkreis Bitburg-Prüm", '["de:regionalschluessel"="07232"]'),
    "bernkastel-wittlich": Region("Bernkastel-Wittlich", '["de:regionalschluessel"="07231"]'),
    "cochem-zell": Region("Cochem-Zell", '["de:regionalschluessel"="07135"]'),
    "ahrweiler": Region("Ahrweiler", '["de:regionalschluessel"="07131"]'),
    "trier": Region("Trier", '["de:regionalschluessel"="07211"]'),
    "trier-saarburg": Region("Trier-Saarburg", '["de:regionalschluessel"="07235"]'),
    "bremen": Region("Bremen", '["de:regionalschluessel"="04011"]'),
    "bremerhaven": Region("Bremerhaven", '["de:regionalschluessel"="04012"]'),
    "cuxhaven": Region("Landkreis Cuxhaven", '["de:regionalschluessel"="03352"]'),
    "wurster-nordseekueste": Region(
        "Wurster Nordseeküste", '["name"="Wurster Nordseeküste"]["boundary"="administrative"]'
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
        f"area{region.filter}->.a;\n"
        f"(\n{koerper}\n);\n"
        "out tags center;"
    )


def abfragen(query: str, leise: bool = False) -> list[dict]:
    """Overpass fragen, mit Spiegeln und Geduld.

    Der Dienst antwortet unter Last mit 429 oder 504. Das ist kein Fehler,
    sondern eine Bitte zu warten - wer sofort nachlegt, wird haerter gedrosselt.
    """
    letzter = ""
    for versuch in range(1, VERSUCHE + 1):
        for endpunkt in ENDPUNKTE:
            host = urlparse(endpunkt).hostname or endpunkt
            try:
                antwort = httpx.post(
                    endpunkt,
                    data={"data": query},
                    timeout=240.0,
                    headers={"User-Agent": USER_AGENT},
                )
            except Exception as exc:  # noqa: BLE001
                letzter = f"{host}: {type(exc).__name__}"
                continue
            if antwort.status_code == 200:
                try:
                    return antwort.json().get("elements", [])
                except ValueError:
                    letzter = f"{host}: unlesbare Antwort"
                    continue
            letzter = f"{host}: HTTP {antwort.status_code}"
            if antwort.status_code not in (429, 503, 504):
                continue
        if versuch < VERSUCHE:
            pause = WARTEN * versuch
            if not leise:
                console.print(
                    f"[yellow]Overpass ist gerade ausgelastet ({letzter}). "
                    f"Warte {pause:.0f}s und versuche es erneut …[/yellow]"
                )
            time.sleep(pause)
    raise RuntimeError(f"Overpass liefert nichts ({letzter}). Spaeter noch einmal versuchen.")


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
    unbekannt = [b for b in branchen if b not in BRANCHEN]
    if unbekannt:
        raise SystemExit(
            f"Unbekannte Branche(n): {', '.join(unbekannt)}. Bekannt: {', '.join(BRANCHEN)}"
        )
    region = REGIONEN[region_name]
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
    p.add_argument("--region", required=True, help="z. B. vulkaneifel")
    p.add_argument(
        "--branchen",
        default="beherbergung,gastronomie,handwerk",
        help="kommagetrennt, Vorgabe: beherbergung,gastronomie,handwerk",
    )
    p.add_argument("--out", type=Path, default=Path("leads.csv"), help="Ziel-CSV")
    p.add_argument("--anhaengen", action="store_true", help="an eine vorhandene CSV anhaengen")
    args = p.parse_args(argv)

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
