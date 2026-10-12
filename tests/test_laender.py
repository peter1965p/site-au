"""Bundesland je Firma, Sperrliste ("nicht passend") und Wiedervorlage - ohne Oberflaeche."""

from __future__ import annotations

import argparse
from datetime import date, timedelta

import pytest

import audit
import finden
import laender
from audit import (
    STATUS_AUSSORTIERT,
    STATUS_ERLEDIGT,
    Cache,
    Fetched,
    Landbuch,
    Lead,
    Result,
    Statusbuch,
    ohne_aussortierte,
    read_leads,
    write_csv,
)
from conftest import load_fixture


# ===========================================================================
# laender.py
# ===========================================================================


@pytest.mark.parametrize(
    "text, iso",
    [
        ("DE-NI", "DE-NI"), ("de-ni", "DE-NI"), ("Niedersachsen", "DE-NI"), ("niedersachsen", "DE-NI"),
        ("NI", "DE-NI"), ("rp", "DE-RP"), ("Rheinland-Pfalz", "DE-RP"), ("Baden-Württemberg", "DE-BW"),
        ("  Bayern  ", "DE-BY"), ("", ""), ("Atlantis", ""), ("XX", ""),
    ],
)
def test_bundesland_aus_name_code_oder_kuerzel(text, iso):
    assert laender.iso_aus(text) == iso


def test_unbekanntes_land_wird_nicht_geraten():
    assert laender.iso_aus("Hessen-Nassau") == ""


def test_alle_sechzehn_laender_sind_umkehrbar():
    assert len(laender.BUNDESLAENDER) == 16
    for iso, name in laender.BUNDESLAENDER.items():
        assert laender.iso_aus(name) == iso
        assert laender.iso_aus(iso) == iso
        assert laender.land_name(iso) == name


def test_land_name_fuer_unbekanntes_ist_der_titel_ohne_bundesland():
    assert laender.land_name("") == "Ohne Bundesland"
    assert laender.land_name("DE-XX") == "Ohne Bundesland"


@pytest.mark.parametrize("schluessel, iso", [("07233", "DE-RP"), ("03352", "DE-NI"), ("040120000000", "DE-HB"),
                                             ("16076", "DE-TH"), ("99999", ""), ("", "")])
def test_land_aus_dem_kreisschluessel(schluessel, iso):
    assert laender.land_aus_schluessel(schluessel) == iso


def test_umlaute_sortieren_wie_der_Grundbuchstabe():
    """Ä wie A, Ö wie O, Ü wie U, ß wie ss - sonst landet "Überlingen" hinter "Zell"."""
    orte = ["Zell", "Überlingen", "Bonn", "Ägypten", "Öhringen", "Oldenburg"]
    assert sorted(orte, key=laender.sortierschluessel) == ["Ägypten", "Bonn", "Öhringen", "Oldenburg", "Überlingen", "Zell"]


def test_finden_bietet_die_tabellen_weiter_an():
    assert finden.BUNDESLAENDER is laender.BUNDESLAENDER
    assert finden.RS_PRAEFIX is laender.RS_PRAEFIX


# ===========================================================================
# CSV und Leads
# ===========================================================================


def _csv(tmp_path, kopf, zeilen):
    p = tmp_path / "l.csv"
    p.write_text(kopf + "\n" + "\n".join(zeilen) + "\n", encoding="utf-8")
    return p


def test_csv_ohne_bundesland_spalte_ergibt_leere_laender(tmp_path):
    leads = read_leads(_csv(tmp_path, "name,url,ort", ["A,a.de,Daun"]))
    assert leads[0].bundesland == ""


def test_csv_liest_bundesland_als_name_code_oder_kuerzel(tmp_path):
    leads = read_leads(_csv(tmp_path, "name,url,ort,bundesland",
                            ["A,a.de,x,Niedersachsen", "B,b.de,x,DE-RP", "C,c.de,x,ni", "D,d.de,x,Atlantis", "E,e.de,x,"]))
    assert [l.bundesland for l in leads] == ["DE-NI", "DE-RP", "DE-NI", "", ""]


def test_ergebnis_csv_hat_eine_bundesland_spalte(tmp_path):
    import csv

    r = Result("A", "a.de", "Daun", "Handwerk", "", "ok", 10.0, "", [], [], {}, "t", bundesland="DE-RP")
    ziel = tmp_path / "e.csv"
    write_csv([r], ziel)
    zeile = next(csv.DictReader(ziel.open(encoding="utf-8"), delimiter=";"))
    assert zeile["bundesland"] == "Rheinland-Pfalz"


def test_ergebnis_csv_laesst_unbekanntes_land_leer(tmp_path):
    import csv

    r = Result("A", "a.de", "", "", "", "ok", 10.0, "", [], [], {}, "t")
    ziel = tmp_path / "e.csv"
    write_csv([r], ziel)
    assert next(csv.DictReader(ziel.open(encoding="utf-8"), delimiter=";"))["bundesland"] == ""


# ===========================================================================
# Landbuch
# ===========================================================================


def test_landbuch_speichert_und_liest(tmp_path):
    lb = Landbuch(tmp_path / "d.sqlite")
    lb.setzen_viele({"https://Firma.de/": "DE-NI", "andere.de": "DE-RP"})
    assert lb.alle() == {"firma.de": "DE-NI", "andere.de": "DE-RP"}, "Schluessel wie bei Cache und Statusbuch"
    lb.close()


def test_landbuch_ueberschreibt_und_ignoriert_leere_codes(tmp_path):
    lb = Landbuch(tmp_path / "d.sqlite")
    lb.setzen_viele({"a.de": "DE-NI"})
    lb.setzen_viele({"a.de": "DE-HB", "b.de": ""})
    assert lb.alle() == {"a.de": "DE-HB"}
    lb.close()


def test_landbuch_teilt_sich_die_datei_mit_cache_und_status(tmp_path):
    db = tmp_path / "d.sqlite"
    cache, status, lb = Cache(db), Statusbuch(db), Landbuch(db)
    lb.setzen_viele({"a.de": "DE-NI"})
    status.setzen("a.de", "angerufen")
    tabellen = {r[0] for r in cache.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"audit", "lead_status", "lead_land"} <= tabellen
    for x in (cache, status, lb):
        x.close()


def test_cache_uebernimmt_das_bundesland_des_leads(tmp_path):
    cache = Cache(tmp_path / "d.sqlite")
    cache.put(Result("A", "a.de", "", "", "", "ok", 10.0, "", [], [], {}, "2026-10-09T10:00:00"), False)
    gelesen = cache.get(Lead("A", "a.de", bundesland="DE-NI"), 0, False)
    assert gelesen.bundesland == "DE-NI"
    cache.close()


def test_cache_behaelt_das_gespeicherte_land_wenn_der_lead_keins_hat(tmp_path):
    cache = Cache(tmp_path / "d.sqlite")
    cache.put(Result("A", "a.de", "", "", "", "ok", 10.0, "", [], [], {}, "2026-10-09T10:00:00", bundesland="DE-RP"), False)
    assert cache.get(Lead("A", "a.de"), 0, False).bundesland == "DE-RP"
    cache.close()


def test_altes_cache_ergebnis_ohne_bundesland_laesst_sich_lesen(tmp_path):
    """Eintraege aus der Zeit vor dem Bundesland-Feld haben den Schluessel nicht."""
    import json

    cache = Cache(tmp_path / "d.sqlite")
    alt = Result("A", "a.de", "", "", "", "ok", 10.0, "", [], [], {}, "2026-10-09T10:00:00").__dict__.copy()
    alt.pop("bundesland")
    cache.conn.execute("INSERT INTO audit (url_key, fetched_at, with_pagespeed, payload) VALUES (?,?,?,?)",
                       ("a.de", "2026-10-09T10:00:00", 0, json.dumps(alt)))
    cache.conn.commit()
    assert cache.get(Lead("A", "a.de"), 0, False).bundesland == ""
    cache.close()


# --- Ergebnisse tragen das Land des Leads --------------------------------------------


class _Attrappe:
    async def robots_allows(self, url):
        return True

    async def get_page(self, kandidaten):
        html = load_fixture("modern_sauber.html")
        return Fetched(url=kandidaten[0], final_url=kandidaten[0] + "/", status=200, html=html, html_bytes=len(html))

    async def status_und_text(self, url):
        return 200, ""

    async def head_status(self, url):
        return 200

    async def text_of(self, url):
        return ""

    async def measure_asset(self, url):
        return None


ARGS = argparse.Namespace(assets=0, pagespeed=False, verzeichnis=False)


async def test_gepruefte_firma_behaelt_ihr_bundesland():
    res = await audit.audit_lead(Lead("A", "a.example", "Daun", bundesland="DE-RP"), _Attrappe(), ARGS, None)
    assert res.bundesland == "DE-RP"


async def test_nicht_erreichbare_firma_behaelt_ihr_bundesland():
    class Tot(_Attrappe):
        async def get_page(self, kandidaten):
            return Fetched(url=kandidaten[0], error="Name or service not known")

    res = await audit.audit_lead(Lead("A", "a.example", bundesland="DE-NI"), Tot(), ARGS, None)
    assert res.status == audit.STATUS_UNERREICHBAR and res.bundesland == "DE-NI"


async def test_robots_gesperrte_firma_behaelt_ihr_bundesland():
    class Gesperrt(_Attrappe):
        async def robots_allows(self, url):
            return False

    res = await audit.audit_lead(Lead("A", "a.example", bundesland="DE-HB"), Gesperrt(), ARGS, None)
    assert res.bundesland == "DE-HB"


# ===========================================================================
# finden.py: das Suchgebiet bestimmt das Land
# ===========================================================================


def test_schnellwahl_kennt_ihr_bundesland():
    assert finden.REGIONEN["vulkaneifel"].land == "DE-RP"
    assert finden.REGIONEN["bremerhaven"].land == "DE-HB"
    assert finden.REGIONEN["cuxhaven"].land == "DE-NI"
    assert finden.REGIONEN["wurster-nordseekueste"].land == "DE-NI"


def test_jede_schnellwahl_hat_ein_land():
    for name, region in finden.REGIONEN.items():
        assert region.land in laender.BUNDESLAENDER, name


def test_firmen_tragen_das_land_des_suchgebiets():
    el = [{"type": "node", "id": 1, "tags": {"name": "Firma", "website": "https://firma.de", "craft": "x"}}]
    assert finden.aufbereiten(el, ["handwerk"], "DE-NI")[0].bundesland == "DE-NI"
    assert finden.aufbereiten(el, ["handwerk"])[0].bundesland == ""


def test_suche_reicht_das_land_der_region_weiter(monkeypatch):
    el = [{"type": "node", "id": 1, "tags": {"name": "Firma", "website": "https://firma.de", "craft": "x"}}]
    monkeypatch.setattr(finden, "abfragen", lambda *a, **k: el)
    firmen = finden.suchen_in(finden.Region("Gebiet", "area(1)", "DE-RP"), ["handwerk"])
    assert firmen[0].bundesland == "DE-RP"


def test_finden_csv_enthaelt_den_namen_des_bundeslands(tmp_path):
    import csv

    ziel = tmp_path / "f.csv"
    finden.schreiben([finden.Firma("A", "a.de", bundesland="DE-NI")], ziel)
    zeile = next(csv.DictReader(ziel.open(encoding="utf-8")))
    assert zeile["bundesland"] == "Niedersachsen"
    assert read_leads(ziel)[0].bundesland == "DE-NI", "und das Tool liest es wieder ein"


# ===========================================================================
# Sperrliste: "nicht passend"
# ===========================================================================


@pytest.fixture
def buch(tmp_path):
    b = Statusbuch(tmp_path / "s.sqlite")
    yield b
    b.close()


def test_aussortierte_sind_die_firmen_mit_status_nicht_passend(buch):
    buch.setzen("a.de", "nicht passend")
    buch.setzen("b.de", "angerufen")
    buch.setzen("https://C.de/", "nicht passend")
    assert buch.aussortierte() == {"a.de", "c.de"}


def test_aussortierte_ist_leer_ohne_eintraege(buch):
    assert buch.aussortierte() == set()


def test_zurueckgeholte_firma_ist_nicht_mehr_gesperrt(buch):
    buch.setzen("a.de", "nicht passend")
    buch.setzen("a.de", "offen")
    assert buch.aussortierte() == set()


def test_ohne_aussortierte_filtert_ueber_den_url_schluessel():
    leads = [Lead("A", "a.de"), Lead("B", "https://B.de/"), Lead("C", "c.de")]
    aktiv, n = ohne_aussortierte(leads, {"a.de", "b.de"})
    assert [l.name for l in aktiv] == ["C"] and n == 2


def test_ohne_aussortierte_ohne_sperrliste_laesst_alles_durch():
    leads = [Lead("A", "a.de")]
    assert ohne_aussortierte(leads, set()) == (leads, 0)


def test_status_aussortiert_ist_im_auswahlfeld_vorhanden():
    assert STATUS_AUSSORTIERT in audit.STATUS_WERTE


# ===========================================================================
# Wiedervorlage: erledigte Faelle erinnern nicht mehr
# ===========================================================================


def _vor(tage):
    return (date.today() - timedelta(days=tage)).isoformat()


def test_faellige_wiedervorlagen_nach_datum_sortiert(buch):
    buch.setzen("neu.de", "wiedervorlage", "", _vor(1))
    buch.setzen("alt.de", "wiedervorlage", "", _vor(5))
    buch.setzen("heute.de", "wiedervorlage", "", _vor(0))
    assert [s.url_key for s in buch.faellige_wiedervorlagen()] == ["alt.de", "neu.de", "heute.de"]


def test_zukuenftige_wiedervorlage_ist_nicht_faellig(buch):
    buch.setzen("spaeter.de", "wiedervorlage", "", _vor(-3))
    assert buch.faellige_wiedervorlagen() == []


@pytest.mark.parametrize("status", sorted(STATUS_ERLEDIGT))
def test_erledigte_faelle_erinnern_nicht_mehr(buch, status):
    """Wer nach der Wiedervorlage abgesagt hat oder aussortiert wurde, soll nicht weiter stoeren."""
    buch.setzen("a.de", status, "", _vor(2))
    assert buch.faellige_wiedervorlagen() == []


@pytest.mark.parametrize("status", ["offen", "angerufen", "wiedervorlage", "termin", "angebot raus"])
def test_offene_faelle_erinnern(buch, status):
    buch.setzen("a.de", status, "", _vor(2))
    assert [s.url_key for s in buch.faellige_wiedervorlagen()] == ["a.de"]


def test_erledigte_status_sind_genau_diese_drei():
    assert STATUS_ERLEDIGT == {"kunde", "kein Interesse", "nicht passend"}
