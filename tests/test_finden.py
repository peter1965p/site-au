"""Tests der Firmensuche - ohne Netzzugriff."""

from __future__ import annotations

import pytest

import finden
from finden import (
    BRANCHEN,
    BUNDESLAENDER,
    RS_PRAEFIX,
    Region,
    aufbereiten,
    bauen,
    kreis_filter,
    land_filter,
)


# --- Gebiete ---------------------------------------------------------------


def test_alle_bundeslaender_vorhanden():
    assert len(BUNDESLAENDER) == 16
    assert BUNDESLAENDER["DE-NI"] == "Niedersachsen"


def test_jedes_bundesland_hat_ein_schluesselpraefix():
    assert set(RS_PRAEFIX) == set(BUNDESLAENDER)
    assert len(set(RS_PRAEFIX.values())) == 16, "Praefixe muessen eindeutig sein"


def test_land_filter_bindet_die_verwaltungsebene():
    """Ohne admin_level greifen auch Regierungsbezirke."""
    f = land_filter("DE-NI")
    assert "DE-NI" in f and 'admin_level"="4' in f


def test_kreis_filter_nutzt_den_regionalschluessel():
    assert kreis_filter("040120000000") == 'area["de:regionalschluessel"~"^040120000000"]'


def test_kreis_filter_trifft_beide_schluessellaengen():
    """OSM fuehrt den Schluessel mal 5-, mal 12-stellig ("04012" gegen
    "040120000000"). Ein exakter Vergleich findet dann nichts."""
    import re as _re

    muster = _re.search(r'"\^([^"]+)"', kreis_filter("04012")).group(1)
    assert _re.match(muster, "04012")
    assert _re.match(muster, "040120000000")


def test_gebietsausdruecke_sind_vollstaendig():
    """Alle Filter muessen mit 'area' beginnen - bauen() setzt nichts davor.

    Fruehere Fassung ergab 'areaarea(...)' und lieferte stumm null Treffer.
    """
    assert land_filter("DE-HB").startswith("area")
    assert kreis_filter("04012").startswith("area")
    for region in finden.REGIONEN.values():
        assert region.filter.startswith("area"), region.titel


# --- Abfrage bauen ---------------------------------------------------------


def test_query_enthaelt_gebiet_und_branchen():
    q = bauen(Region("Test", 'area["x"="y"]'), ["handwerk"])
    assert 'area["x"="y"]->.a;' in q
    assert '["craft"]' in q
    assert "out tags center;" in q


def test_query_setzt_kein_zweites_area_davor():
    q = bauen(Region("Test", "area(3600062658)"), ["gastronomie"])
    assert "areaarea" not in q
    assert "area(3600062658)->.a;" in q


def test_query_filtert_auf_vorhandene_website():
    """Ohne Website gibt es nichts zu pruefen."""
    q = bauen(Region("T", "area(1)"), ["handwerk"])
    assert q.count('["website"]') >= 1


def test_mehrere_branchen_ergeben_mehrere_zeilen():
    eine = bauen(Region("T", "area(1)"), ["handwerk"])
    zwei = bauen(Region("T", "area(1)"), ["handwerk", "gastronomie"])
    assert zwei.count("nwr") > eine.count("nwr")


def test_unbekannte_branche_wird_abgelehnt():
    with pytest.raises(SystemExit, match="Unbekannte Branche"):
        finden.suchen_in(Region("T", "area(1)"), ["gibtsnicht"])


def test_ohne_branche_keine_abfrage():
    """Sonst entsteht eine leere Gruppe, die alles im Gebiet zurueckgibt."""
    assert finden.suchen_in(Region("T", "area(1)"), []) == []


# --- Ergebnisse aufbereiten ------------------------------------------------


def _el(name, website, **tags):
    return {"type": "node", "id": 1, "tags": {"name": name, "website": website, **tags}}


def test_firma_wird_uebernommen():
    f = aufbereiten([_el("Bäckerei Roden", "https://www.landbaeckerei-roden.de/", craft="bakery",
                         **{"addr:city": "Jünkerath", "phone": "06597 123"})], ["handwerk"])
    assert len(f) == 1
    assert f[0].url == "landbaeckerei-roden.de", "Schema und www fallen weg"
    assert f[0].ort == "Jünkerath"
    assert f[0].telefon == "06597 123"


def test_website_ohne_schema_wird_ergaenzt():
    f = aufbereiten([_el("Firma", "firma.de", craft="carpenter")], ["handwerk"])
    assert f[0].url == "firma.de"


def test_eintrag_ohne_website_faellt_weg():
    assert aufbereiten([{"type": "node", "id": 1, "tags": {"name": "Ohne"}}], ["handwerk"]) == []


def test_eintrag_ohne_namen_faellt_weg():
    assert aufbereiten([_el("", "https://firma.de", craft="x")], ["handwerk"]) == []


def test_plattformseiten_fallen_weg():
    """Eine Facebook-Seite ist nicht die Website der Firma."""
    elemente = [
        _el("A", "https://www.facebook.com/firma", craft="x"),
        _el("B", "https://www.booking.com/hotel", tourism="hotel"),
        _el("C", "https://echte-firma.de", craft="x"),
    ]
    assert [f.url for f in aufbereiten(elemente, ["handwerk"])] == ["echte-firma.de"]


def test_dubletten_werden_zusammengefasst():
    """Mehrere OSM-Eintraege derselben Firma ergeben einen Lead."""
    elemente = [
        _el("Firma Filiale 1", "https://firma.de", craft="x"),
        _el("Firma Filiale 2", "https://www.firma.de/kontakt", craft="x", phone="0123 456"),
    ]
    f = aufbereiten(elemente, ["handwerk"])
    assert len(f) == 1
    assert f[0].telefon == "0123 456", "fehlende Angaben werden ergaenzt"


def test_kontaktdaten_aus_contact_tags():
    f = aufbereiten([_el("Firma", "https://firma.de", craft="x",
                         **{"contact:phone": "0123", "contact:email": "a@b.de"})], ["handwerk"])
    assert f[0].telefon == "0123"
    assert f[0].email == "a@b.de"


def test_mailto_praefix_faellt_weg():
    f = aufbereiten([_el("Firma", "https://firma.de", craft="x", email="mailto:a@b.de")], ["handwerk"])
    assert f[0].email == "a@b.de"


def test_kaputte_adresse_kippt_nicht():
    f = aufbereiten([_el("Firma", "https:///", craft="x"), _el("Zwei", "nicht mal eine url", craft="x")],
                    ["handwerk"])
    assert all("." in x.url for x in f)


# --- Branchen --------------------------------------------------------------


def test_jede_branche_hat_titel_und_selektoren():
    for schluessel, (titel, selektoren) in BRANCHEN.items():
        assert titel and selektoren
        assert all(sel.startswith("[") for sel in selektoren), schluessel
