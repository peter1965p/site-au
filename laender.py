"""Die 16 Bundeslaender: Namen, Schluesselpraefixe und Umrechnungen.

Eigenes Modul, damit audit.py, finden.py und gui.py dieselbe Tabelle benutzen und
nicht drei Kopien pflegen muessen.
"""

from __future__ import annotations

# ISO-3166-2-Codes mit Namen
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

# Erste zwei Ziffern des Regionalschluessels (amtlicher Gemeindeschluessel) je Land.
# Darueber lassen sich die Kreise eines Landes zuverlaessig greifen - die naheliegende
# Abfrage "alle Relationen in der Flaeche" liefert auch Kreise der Nachbarn.
RS_PRAEFIX: dict[str, str] = {
    "DE-SH": "01", "DE-HH": "02", "DE-NI": "03", "DE-HB": "04",
    "DE-NW": "05", "DE-HE": "06", "DE-RP": "07", "DE-BW": "08",
    "DE-BY": "09", "DE-SL": "10", "DE-BE": "11", "DE-BB": "12",
    "DE-MV": "13", "DE-SN": "14", "DE-ST": "15", "DE-TH": "16",
}

OHNE_LAND = ""  # steht fuer "Bundesland unbekannt"
OHNE_LAND_TITEL = "Ohne Bundesland"

_NACH_PRAEFIX = {praefix: iso for iso, praefix in RS_PRAEFIX.items()}
_NACH_NAME = {name.casefold(): iso for iso, name in BUNDESLAENDER.items()}
_NACH_KUERZEL = {iso[3:].casefold(): iso for iso in BUNDESLAENDER}


def land_aus_schluessel(schluessel: str) -> str:
    """Bundesland (ISO-Code) zu einem Regional- oder Kreisschluessel, sonst ''."""
    return _NACH_PRAEFIX.get((schluessel or "").strip()[:2], OHNE_LAND)


def iso_aus(text: str) -> str:
    """Bundesland-Code aus ISO-Code, Namen oder Kuerzel ("DE-NI", "Niedersachsen", "ni").

    Unbekanntes ergibt '' - lieber "ohne Bundesland" als eine geratene Zuordnung.
    """
    t = (text or "").strip()
    if not t:
        return OHNE_LAND
    if t.upper() in BUNDESLAENDER:
        return t.upper()
    return _NACH_NAME.get(t.casefold()) or _NACH_KUERZEL.get(t.casefold()) or OHNE_LAND


def land_name(iso: str) -> str:
    """Anzeigename; fuer unbekannte Codes der Titel 'Ohne Bundesland'."""
    return BUNDESLAENDER.get(iso, OHNE_LAND_TITEL)


def sortierschluessel(text: str) -> str:
    """Alphabetische Ordnung mit deutscher Umlautregel (Ä wie A, ß wie ss), unabhaengig von der Locale."""
    t = text.casefold()
    for alt, neu in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss")):
        t = t.replace(alt, neu)
    return t
