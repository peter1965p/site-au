"""Tests des Akquise-Statusbuchs."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import audit
from audit import Cache, Lead, LeadStatus, Result, Statusbuch, write_csv


@pytest.fixture
def buch(tmp_path):
    b = Statusbuch(tmp_path / "s.sqlite")
    yield b
    b.close()


def test_unbekannter_lead_ist_offen(buch):
    st = buch.get("beispiel.de")
    assert st.status == "offen"
    assert st.offen
    assert st.notiz == ""
    assert st.wiedervorlage == ""


def test_status_wird_gespeichert_und_gelesen(buch):
    buch.setzen("beispiel.de", "angerufen", "Chef erst ab 16 Uhr erreichbar")
    st = buch.get("beispiel.de")
    assert st.status == "angerufen"
    assert st.notiz == "Chef erst ab 16 Uhr erreichbar"
    assert st.geaendert_am, "Zeitstempel wird mitgeschrieben"
    assert not st.offen


def test_status_ignoriert_schema_und_schraegstrich(buch):
    buch.setzen("https://Beispiel.DE/", "termin")
    assert buch.get("beispiel.de").status == "termin"


def test_status_wird_ueberschrieben(buch):
    buch.setzen("beispiel.de", "angerufen", "erste Notiz")
    buch.setzen("beispiel.de", "kein Interesse", "zweite Notiz")
    st = buch.get("beispiel.de")
    assert st.status == "kein Interesse"
    assert st.notiz == "zweite Notiz"


def test_alle_liefert_eine_zuordnung(buch):
    buch.setzen("a.de", "angerufen")
    buch.setzen("b.de", "termin")
    alle = buch.alle()
    assert set(alle) == {"a.de", "b.de"}
    assert alle["b.de"].status == "termin"


def test_faellige_wiedervorlagen(buch):
    gestern = (date.today() - timedelta(days=1)).isoformat()
    morgen = (date.today() + timedelta(days=1)).isoformat()
    buch.setzen("faellig.de", "wiedervorlage", "", gestern)
    buch.setzen("heute.de", "wiedervorlage", "", date.today().isoformat())
    buch.setzen("spaeter.de", "wiedervorlage", "", morgen)
    buch.setzen("ohne.de", "angerufen", "", "")

    faellig = {s.url_key for s in buch.faellige_wiedervorlagen()}
    assert faellig == {"faellig.de", "heute.de"}, "heute ist faellig, morgen nicht"


def test_faellige_sind_nach_datum_sortiert(buch):
    buch.setzen("b.de", "wiedervorlage", "", "2026-05-01")
    buch.setzen("a.de", "wiedervorlage", "", "2026-01-01")
    assert [s.url_key for s in buch.faellige_wiedervorlagen("2026-12-31")] == ["a.de", "b.de"]


def test_status_ueberlebt_einen_refresh_lauf(tmp_path):
    """Der entscheidende Punkt: --refresh wirft Pruefergebnisse weg, aber
    niemals die Notiz aus einem Telefonat."""
    db = tmp_path / "db.sqlite"
    buch = Statusbuch(db)
    buch.setzen("beispiel.de", "termin", "Rückruf am Montag vereinbart")

    cache = Cache(db)
    res = Result(
        name="Beispiel", url="beispiel.de", ort="", branche="", final_url="https://beispiel.de",
        status="ok", score=50.0, cms="Jimdo", argumente=[], befunde=[], severities={},
        geprueft_am="2026-10-08T10:00:00",
    )
    cache.put(res, False)
    # Ein neuer Lauf ueberschreibt das Pruefergebnis vollstaendig.
    res.score = 12.0
    cache.put(res, False)
    cache.close()

    nachher = Statusbuch(db).get("beispiel.de")
    assert nachher.status == "termin"
    assert nachher.notiz == "Rückruf am Montag vereinbart"


def test_cache_und_statusbuch_teilen_sich_eine_datei(tmp_path):
    db = tmp_path / "db.sqlite"
    cache = Cache(db)
    buch = Statusbuch(db)
    buch.setzen("beispiel.de", "angerufen")
    tabellen = {
        r[0] for r in cache.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"audit", "lead_status"} <= tabellen
    cache.close()
    buch.close()


def test_status_landet_in_der_csv(tmp_path):
    ziel = tmp_path / "ergebnis.csv"
    res = Result(
        name="Beispiel", url="beispiel.de", ort="Daun", branche="Handwerk",
        final_url="https://beispiel.de", status="ok", score=50.0, cms="",
        argumente=["Argument"], befunde=["Befund"], severities={},
        geprueft_am="2026-10-08T10:00:00",
    )
    status = {"beispiel.de": LeadStatus("beispiel.de", "termin", "Notiz dazu", "2026-11-01")}
    write_csv([res], ziel, status)
    inhalt = ziel.read_text(encoding="utf-8")
    assert "termin" in inhalt
    assert "Notiz dazu" in inhalt
    assert "2026-11-01" in inhalt


def test_csv_ohne_statusbuch_zeigt_offen(tmp_path):
    """Ein Lead ohne Eintrag ist offen - das ist die richtige Aussage,
    eine leere Zelle waere mehrdeutig."""
    ziel = tmp_path / "ergebnis.csv"
    res = Result(
        name="B", url="b.de", ort="", branche="", final_url="", status="ok", score=1.0,
        cms="", argumente=[], befunde=[], severities={}, geprueft_am="2026-10-08T10:00:00",
    )
    write_csv([res], ziel)
    kopf, zeile = ziel.read_text(encoding="utf-8").splitlines()[:2]
    spalten = kopf.split(";")
    assert zeile.split(";")[spalten.index("akquise")] == "offen"
    assert zeile.split(";")[spalten.index("notiz")] == ""


def test_alle_statuswerte_sind_eindeutig():
    assert len(audit.STATUS_WERTE) == len(set(audit.STATUS_WERTE))
    assert audit.STATUS_WERTE[0] == "offen", "offen ist der Ausgangszustand"
