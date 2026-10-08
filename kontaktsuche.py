#!/usr/bin/env python3
"""Kontaktdaten zu einer Firma finden, wenn die Website nichts hergibt.

Getrennt nach dem, was erlaubt ist:

* **Automatisch** wird nur Das Örtliche abgefragt. Dessen robots.txt gibt
  Crawlern ausdruecklich alles frei (`Disallow:` ohne Pfad), und pro Lead
  faellt genau eine Anfrage an - das ist Nachschlagen, kein Abgrasen.
* **Nicht automatisch** gehen Gelbe Seiten (robots.txt sperrt `/suche`) und
  Google (die Nutzungsbedingungen untersagen automatisierte Abfragen, und
  nach wenigen Versuchen kommt ohnehin ein Captcha). Fuer diese Quellen
  liefert das Modul nur fertige Links, die der Mensch im eigenen Browser
  oeffnet. Das ist Nachschlagen von Hand und voellig unbedenklich.

Unabhaengig von robots.txt gilt: Verzeichnisse sind als Datenbank
geschuetzt (§ 87b UrhG). Einzelne Abfragen zu einem konkreten Lead sind
etwas anderes als das systematische Auslesen des Bestands - dieses Modul
tut bewusst nur Ersteres und hat keine Funktion, die einen ganzen Ort
durchlaeuft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import quote_plus

from selectolax.lexbor import LexborHTMLParser

OERTLICHE = "https://www.dasoertliche.de/"

# Ab dieser Namensaehnlichkeit gilt ein Treffer als dieselbe Firma. Lieber
# nichts liefern als die Nummer des Nachbarbetriebs - danach waehlt jemand.
MIN_AEHNLICHKEIT = 0.55

RECHTSFORMEN = (
    "gmbh", "co", "kg", "ag", "ohg", "ug", "mbh", "e.k.", "ek", "gbr",
    "und", "&", "inhaber", "inh", "meisterbetrieb", "fachgeschäft",
    "fachgeschaeft", "betrieb", "service",
)


@dataclass
class Kontakttreffer:
    name: str
    telefon: str = ""
    adresse: str = ""
    quelle: str = ""
    aehnlichkeit: float = 0.0


UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _normal(text: str) -> str:
    """Kleinschreibung und Umlaute vereinheitlichen.

    In Lead-Listen steht mal "Müllenborn", mal "Muellenborn". Ohne das
    greift der Ortsabgleich genau dort nicht, wo er gebraucht wird.
    """
    return text.lower().translate(UMLAUTE)


def _kennwoerter(text: str) -> set[str]:
    """Bedeutungstragende Wortteile eines Firmennamens."""
    woerter = re.findall(r"[\wäöüßÄÖÜ]{3,}", _normal(text))
    return {w for w in woerter if w not in RECHTSFORMEN}


def ort_passt(gesucht: str, adresse: str) -> bool:
    """Liegt der Treffer im gesuchten Ort?

    Ohne diese Pruefung liefert ein haeufiger Firmenname wie "Peter Mueller"
    irgendeinen Betrieb aus dem Bundesgebiet - und jemand waehlt diese
    Nummer. Lieber keine Nummer als die eines fremden Unternehmens.
    """
    if not gesucht:
        return True  # ohne Ortsangabe laesst sich nichts pruefen
    if not adresse:
        return False
    ziel = _normal(adresse)
    # Ortsteile und Schreibvarianten: jedes laengere Wort des gesuchten Orts
    # muss vorkommen, "Bad Neuenahr-Ahrweiler" also auch als Teil.
    teile = [w for w in re.split(r"[\s,/()-]+", _normal(gesucht)) if len(w) > 3]
    return any(t in ziel for t in teile) if teile else _normal(gesucht) in ziel


def namen_passen(gesucht: str, gefunden: str) -> float:
    """Wie gut zwei Firmennamen zusammenpassen, 0 bis 1.

    Reiner Zeichenvergleich reicht nicht: "Peter Müller" und "Fleischer-
    Fachgeschäft Peter Müller GmbH Metzgerei" sind dieselbe Firma, als
    Zeichenketten aber weit auseinander. Deshalb zaehlt zusaetzlich, wie
    viele der gesuchten Kennwoerter im Fundstueck vorkommen.
    """
    a, b = gesucht.lower().strip(), gefunden.lower().strip()
    if not a or not b:
        return 0.0
    zeichen = SequenceMatcher(None, a, b).ratio()
    ka, kb = _kennwoerter(a), _kennwoerter(b)
    if not ka:
        return zeichen
    deckung = len(ka & kb) / len(ka)
    return max(zeichen, deckung)


TEL_PUTZEN = re.compile(r"(?i)\b(tel\.?|telefon|gratis anrufen!?|anrufen)\b")


def _telefon_saeubern(roh: str) -> str:
    text = TEL_PUTZEN.sub(" ", roh)
    treffer = re.search(r"(?:\+49|0)[\d\s/()-]{6,24}\d", text)
    return " ".join(treffer.group(0).split()) if treffer else ""


def oertliche_auswerten(html: str, gesucht: str, ort: str = "") -> Kontakttreffer | None:
    """Trefferliste von Das Örtliche auswerten.

    Ein Treffer wird nur uebernommen, wenn Name **und** Ort passen.
    """
    try:
        baum = LexborHTMLParser(html)
    except Exception:  # noqa: BLE001
        return None

    bester: Kontakttreffer | None = None
    for hit in baum.css("div.hit")[:8]:
        kopf = hit.css_first("h2") or hit.css_first("[class*=name]")
        if kopf is None:
            continue
        name = " ".join(kopf.text(separator=" ", strip=True).split())
        if not name:
            continue
        guete = namen_passen(gesucht, name)
        if guete < MIN_AEHNLICHKEIT or (bester and guete <= bester.aehnlichkeit):
            continue

        tel_node = hit.css_first("[class*=phone]") or hit.css_first("[class*=tel]")
        adr_node = hit.css_first("address") or hit.css_first("[class*=adr]")
        adresse = " ".join(adr_node.text(separator=" ", strip=True).split()) if adr_node else ""

        if not ort_passt(ort, adresse):
            continue

        bester = Kontakttreffer(
            name=name,
            telefon=_telefon_saeubern(tel_node.text(separator=" ", strip=True)) if tel_node else "",
            adresse=adresse,
            quelle="Das Örtliche",
            aehnlichkeit=round(guete, 2),
        )
    return bester if bester and bester.telefon else None


def oertliche_url(name: str, ort: str) -> str:
    return f"{OERTLICHE}?form_name=search_nat&kw={quote_plus(name)}&ci={quote_plus(ort)}"


async def oertliche_suchen(name: str, ort: str, fetcher) -> Kontakttreffer | None:
    """Eine Anfrage an Das Örtliche, robots.txt wird vorher geprueft.

    `fetcher` ist ein audit.Fetcher - damit gelten dieselbe Hoeflichkeit
    (ein Request pro Sekunde und Host) und derselbe ehrliche User-Agent
    wie beim Pruefen der Websites.
    """
    if not name:
        return None
    adresse = oertliche_url(name, ort)
    try:
        if not await fetcher.robots_allows(adresse):
            return None
        html = await fetcher.text_of(adresse)
    except Exception:  # noqa: BLE001 - eine Zusatzsuche darf nie den Lauf kippen
        return None
    return oertliche_auswerten(html, name, ort) if html else None


# ---------------------------------------------------------------------------
# Quellen zum Selbernachschlagen
# ---------------------------------------------------------------------------


def such_links(name: str, ort: str = "", website: str = "") -> list[tuple[str, str]]:
    """Fertige Suchadressen fuer den Browser des Nutzers.

    Diese Seiten duerfen nicht automatisiert abgefragt werden, von Hand
    nachschlagen darf dort aber jeder.
    """
    firma = name.strip()
    mit_ort = f"{firma} {ort}".strip()
    links = [
        ("Das Örtliche", oertliche_url(firma, ort)),
        ("Gelbe Seiten", f"https://www.gelbeseiten.de/suche/{quote_plus(firma)}/{quote_plus(ort or 'deutschland')}"),
        ("11880", f"https://www.11880.com/suche/{quote_plus(firma)}/{quote_plus(ort or 'deutschland')}"),
        ("Google", f"https://www.google.com/search?q={quote_plus(mit_ort + ' Telefon Impressum')}"),
    ]
    if website:
        blank = website.removeprefix("https://").removeprefix("http://").rstrip("/")
        links.append(
            ("Impressum über Google", f"https://www.google.com/search?q={quote_plus('site:' + blank + ' impressum')}")
        )
    return links
