"""Tests zu Kreislisten und Overpass-Abfragen: Zeitbudget, mitgelieferte Daten. Ohne Netz."""

from __future__ import annotations

import json

import httpx
import pytest

import finden
from finden import KEINE_KREISE, RS_PRAEFIX, abfragen, kreise, kreise_lokal, wartezeit

OHNE_NETZ = "Diese Funktion darf keinen Netzzugriff ausloesen"


@pytest.fixture
def dateien(tmp_path, monkeypatch):
    """Cache- und Beilage-Datei auf Wegwerfpfade umbiegen."""
    cache, beilage = tmp_path / "cache.json", tmp_path / "kreise_de.json"
    monkeypatch.setattr(finden, "KREIS_CACHE", cache)
    monkeypatch.setattr(finden, "KREISE_DATEI", beilage)
    return cache, beilage


@pytest.fixture
def kein_sleep(monkeypatch):
    """Haelt fest, wie lange gewartet worden waere, ohne zu warten."""
    gewartet: list[float] = []
    monkeypatch.setattr(finden.time, "sleep", lambda s: gewartet.append(s))
    return gewartet


# ===========================================================================
# Kreislisten ohne Netz
# ===========================================================================


def test_beigelegte_liste_wird_ohne_netz_gelesen(dateien, monkeypatch):
    _cache, beilage = dateien
    beilage.write_text(json.dumps({"DE-RP": [["Vulkaneifel", "07233"], ["Ahrweiler", "07131"]]}), encoding="utf-8")
    monkeypatch.setattr(finden, "abfragen", lambda *a, **k: pytest.fail(OHNE_NETZ))
    assert kreise_lokal("DE-RP") == [("Vulkaneifel", "07233"), ("Ahrweiler", "07131")]
    assert kreise("DE-RP") == [("Vulkaneifel", "07233"), ("Ahrweiler", "07131")]


def test_lokaler_cache_hat_vorrang_vor_der_beilage(dateien):
    cache, beilage = dateien
    beilage.write_text(json.dumps({"DE-RP": [["alt", "07001"]]}), encoding="utf-8")
    cache.write_text(json.dumps({"DE-RP": [["neu", "07002"]]}), encoding="utf-8")
    assert kreise_lokal("DE-RP") == [("neu", "07002")]


def test_fehlende_liste_ergibt_none(dateien):
    assert kreise_lokal("DE-RP") is None


def test_kaputte_datei_ist_wie_keine_datei(dateien):
    _cache, beilage = dateien
    beilage.write_text("{das ist kein json", encoding="utf-8")
    assert kreise_lokal("DE-RP") is None


def test_leerer_eintrag_zaehlt_nicht(dateien):
    _cache, beilage = dateien
    beilage.write_text(json.dumps({"DE-RP": []}), encoding="utf-8")
    assert kreise_lokal("DE-RP") is None


def test_stadtstaaten_haben_keine_kreise_und_brauchen_kein_netz(dateien, monkeypatch):
    monkeypatch.setattr(finden, "abfragen", lambda *a, **k: pytest.fail(OHNE_NETZ))
    for iso in ("DE-BE", "DE-HH"):
        assert kreise_lokal(iso) == []
        assert kreise(iso) == []
    assert KEINE_KREISE == {"DE-BE", "DE-HH"}


def test_ohne_beilage_wird_im_netz_gefragt_mit_zeitbudget_und_gemerkt(dateien, monkeypatch):
    cache, _beilage = dateien
    gesehen = {}

    def falsche_abfrage(query, leise=False, max_sekunden=None):
        gesehen["budget"] = max_sekunden
        gesehen["query"] = query
        return [
            {"tags": {"name": "Zeta", "de:regionalschluessel": "07999"}},
            {"tags": {"name": "Alpha", "de:regionalschluessel": "07111"}},
            {"tags": {"name": "Doppelt", "de:regionalschluessel": "07111"}},  # gleicher Schluessel
            {"tags": {"name": "", "de:regionalschluessel": "07555"}},  # ohne Namen
        ]

    monkeypatch.setattr(finden, "abfragen", falsche_abfrage)
    erg = kreise("DE-RP")
    assert erg == [("Alpha", "07111"), ("Zeta", "07999")], "sortiert, ohne Dubletten und namenlose"
    assert gesehen["budget"] is not None and gesehen["budget"] <= 90, "Dialoge duerfen nicht ewig warten"
    assert '"^07"' in gesehen["query"], "Praefix von Rheinland-Pfalz"
    assert json.loads(cache.read_text(encoding="utf-8"))["DE-RP"], "fuers naechste Mal gemerkt"


# ===========================================================================
# Die mitgelieferte Datei
# ===========================================================================


@pytest.fixture(scope="module")
def beilage_echt():
    return json.loads(finden.KREISE_DATEI.read_text(encoding="utf-8"))


def test_beilage_ist_vorhanden_und_hat_ein_datum(beilage_echt):
    assert beilage_echt.get("_stand"), "wann die Liste gezogen wurde"


def test_beilage_deckt_alle_laender_mit_kreisebene_ab(beilage_echt):
    for iso in RS_PRAEFIX:
        if iso in KEINE_KREISE:
            continue
        assert beilage_echt.get(iso), f"{iso} fehlt"


@pytest.mark.parametrize(
    "iso, anzahl",
    [
        ("DE-BW", 44), ("DE-BY", 96), ("DE-BB", 18), ("DE-HB", 2), ("DE-HE", 26),
        ("DE-MV", 8), ("DE-NI", 45), ("DE-NW", 53), ("DE-RP", 36), ("DE-SL", 6),
        ("DE-SN", 13), ("DE-ST", 14), ("DE-SH", 15), ("DE-TH", 22),
    ],
)
def test_beilage_hat_die_bekannte_zahl_von_kreisen(beilage_echt, iso, anzahl):
    assert len(beilage_echt[iso]) == anzahl


def test_beilage_gesamtzahl_passt_zu_deutschland(beilage_echt):
    """Deutschland hat 400 Kreise und kreisfreie Staedte, davon Berlin und Hamburg
    ohne eigene Kreisebene in dieser Zaehlung."""
    gesamt = sum(len(v) for k, v in beilage_echt.items() if k in RS_PRAEFIX)
    assert gesamt == 398, gesamt  # 400 minus Berlin und Hamburg


def test_beilage_schluessel_gehoeren_zum_richtigen_land(beilage_echt):
    for iso, eintraege in beilage_echt.items():
        if iso not in RS_PRAEFIX:
            continue
        for name, rs in eintraege:
            assert rs.startswith(RS_PRAEFIX[iso]), f"{name} ({rs}) liegt nicht in {iso}"


def test_beilage_hat_keine_doppelten_schluessel(beilage_echt):
    alle = [rs for k, v in beilage_echt.items() if k in RS_PRAEFIX for _n, rs in v]
    assert len(alle) == len(set(alle))


def test_beilage_ist_alphabetisch_je_land(beilage_echt):
    for iso, eintraege in beilage_echt.items():
        if iso in RS_PRAEFIX:
            namen = [n.lower() for n, _ in eintraege]
            assert namen == sorted(namen), iso


def test_vulkaneifel_ist_in_der_beilage_und_passt_zur_schnellwahl(beilage_echt):
    rs = dict((n, r) for n, r in beilage_echt["DE-RP"])
    treffer = [r for n, r in rs.items() if "vulkaneifel" in n.lower()]
    assert treffer, "Landkreis Vulkaneifel fehlt"
    assert '"^07233"' in finden.REGIONEN["vulkaneifel"].filter
    assert treffer[0].startswith("07233")


# ===========================================================================
# Overpass-Abfragen: Zeitbudget und ehrliche Meldungen
# ===========================================================================


def _alle_spiegel_tot(monkeypatch, status=None):
    def post(url, **kw):
        if status is None:
            raise httpx.ConnectError("weg")
        return httpx.Response(status, request=httpx.Request("POST", url), text="x")

    monkeypatch.setattr(finden.httpx, "post", post)
    monkeypatch.setattr(finden, "wartezeit", lambda: 0)


def test_zeitbudget_verhindert_das_lange_warten(monkeypatch, kein_sleep):
    """Ohne Budget: 3 Runden mit 25 s und 50 s Pause. Mit Budget darf nichts davon passieren."""
    _alle_spiegel_tot(monkeypatch, status=504)
    with pytest.raises(RuntimeError):
        abfragen("q", leise=True, max_sekunden=5)
    assert sum(kein_sleep) <= 5, kein_sleep


def test_ohne_budget_wird_wie_bisher_gewartet(monkeypatch, kein_sleep):
    _alle_spiegel_tot(monkeypatch, status=504)
    with pytest.raises(RuntimeError):
        abfragen("q", leise=True)
    assert sum(kein_sleep) >= finden.WARTEN


def test_fehlermeldung_nennt_alle_server(monkeypatch, kein_sleep):
    """Frueher stand nur der zuletzt befragte, tote Spiegel da - der Hauptserver,
    an dem es hing, wurde verschwiegen."""
    def post(url, **kw):
        code = 504 if "overpass-api.de" in url else 500
        return httpx.Response(code, request=httpx.Request("POST", url), text="x")

    monkeypatch.setattr(finden.httpx, "post", post)
    monkeypatch.setattr(finden, "wartezeit", lambda: 0)
    with pytest.raises(RuntimeError) as exc:
        abfragen("q", leise=True, max_sekunden=5)
    text = str(exc.value)
    assert "overpass-api.de: HTTP 504" in text
    assert "HTTP 500" in text
    assert "gespendeter Dienst" in text


def test_lange_serverwartezeit_sprengt_das_budget_sofort(monkeypatch, kein_sleep):
    monkeypatch.setattr(finden, "wartezeit", lambda: 120)
    monkeypatch.setattr(finden.httpx, "post", lambda *a, **k: pytest.fail("kein Abruf bei Sperre"))
    with pytest.raises(RuntimeError, match="ausgelastet"):
        abfragen("q", leise=True, max_sekunden=10)
    assert kein_sleep == []


def test_erfolgreiche_abfrage_liefert_die_elemente(monkeypatch):
    monkeypatch.setattr(finden, "wartezeit", lambda: 0)
    monkeypatch.setattr(
        finden.httpx, "post",
        lambda url, **kw: httpx.Response(200, request=httpx.Request("POST", url), json={"elements": [{"id": 1}]}),
    )
    assert abfragen("q", leise=True, max_sekunden=30) == [{"id": 1}]


def test_spiegel_mit_html_statt_json_wird_uebersprungen(monkeypatch):
    def post(url, **kw):
        if "overpass-api.de" in url:
            return httpx.Response(200, request=httpx.Request("POST", url), text="<html>Fehler</html>")
        return httpx.Response(200, request=httpx.Request("POST", url), json={"elements": [{"id": 2}]})

    monkeypatch.setattr(finden.httpx, "post", post)
    monkeypatch.setattr(finden, "wartezeit", lambda: 0)
    assert abfragen("q", leise=True, max_sekunden=30) == [{"id": 2}]


def test_zeitgrenze_je_anfrage_richtet_sich_nach_dem_budget(monkeypatch):
    gesehen = []

    def post(url, **kw):
        gesehen.append(kw["timeout"])
        return httpx.Response(200, request=httpx.Request("POST", url), json={"elements": []})

    monkeypatch.setattr(finden.httpx, "post", post)
    monkeypatch.setattr(finden, "wartezeit", lambda: 0)
    abfragen("q", leise=True, max_sekunden=20)
    assert gesehen[0] <= 20, "eine einzelne Anfrage darf das Budget nicht sprengen"
    gesehen.clear()
    abfragen("q", leise=True)
    assert gesehen[0] == 240.0, "ohne Budget bleibt es bei der grosszuegigen Grenze"


# --- Statusabfrage ---------------------------------------------------------


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("Connected as: 1\n2 slots available now.\n", 0),
        ("Connected as: 1\n0 slots available now.\nSlot available after: 2026-10-09T12:00:00Z, in 25 seconds.\n", 25),
        ("Slot available after: x, in 40 seconds.\nSlot available after: y, in 12 seconds.\n", 12),
        ("irgendwas Unerwartetes", 0),
    ],
)
def test_wartezeit_aus_dem_status(monkeypatch, text, erwartet):
    monkeypatch.setattr(
        finden.httpx, "get", lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), text=text)
    )
    assert wartezeit() == erwartet


def test_wartezeit_bei_ausfall_des_status_ist_null(monkeypatch):
    def get(url, **kw):
        raise httpx.ConnectError("weg")

    monkeypatch.setattr(finden.httpx, "get", get)
    assert wartezeit() == 0


# ===========================================================================
# Kreislisten erneuern
# ===========================================================================


def _falsche_kreise(iso, erzwingen=False, max_sekunden=None):
    return [(f"Kreis {iso}", RS_PRAEFIX[iso] + "001")]


def test_erneuern_schreibt_alle_laender_mit_datum(tmp_path, monkeypatch):
    monkeypatch.setattr(finden, "kreise", _falsche_kreise)
    monkeypatch.setattr(finden.time, "sleep", lambda s: None)
    ziel = tmp_path / "k.json"
    erg = finden.kreise_erneuern(ziel)
    daten = json.loads(ziel.read_text(encoding="utf-8"))
    assert set(RS_PRAEFIX) <= set(daten)
    assert daten["DE-BE"] == [] and daten["DE-HH"] == [], "Stadtstaaten ohne Kreise"
    assert daten["DE-RP"] == [["Kreis DE-RP", "07001"]]
    assert daten["_stand"]
    assert erg["_fehlgeschlagen"] == []


def test_erneuern_behaelt_den_alten_stand_wenn_ein_land_scheitert(tmp_path, monkeypatch):
    ziel = tmp_path / "k.json"
    ziel.write_text(json.dumps({"DE-RP": [["Alt", "07111"]], "_stand": "2020-01-01"}), encoding="utf-8")

    def teils(iso, erzwingen=False, max_sekunden=None):
        if iso == "DE-RP":
            raise RuntimeError("Dienst weg")
        return _falsche_kreise(iso)

    monkeypatch.setattr(finden, "kreise", teils)
    monkeypatch.setattr(finden.time, "sleep", lambda s: None)
    erg = finden.kreise_erneuern(ziel)
    daten = json.loads(ziel.read_text(encoding="utf-8"))
    assert daten["DE-RP"] == [["Alt", "07111"]], "Rheinland-Pfalz bleibt wie es war"
    assert erg["_fehlgeschlagen"] == ["DE-RP"]
    assert daten["_stand"] == "2020-01-01", "unvollstaendig = Datum nicht erneuern"


def test_erneuern_haelt_pausen_zwischen_den_abfragen(tmp_path, monkeypatch):
    pausen: list[float] = []
    monkeypatch.setattr(finden, "kreise", _falsche_kreise)
    monkeypatch.setattr(finden.time, "sleep", lambda s: pausen.append(s))
    finden.kreise_erneuern(tmp_path / "k.json", pause=4.0)
    assert len(pausen) == len(RS_PRAEFIX) - len(KEINE_KREISE)
    assert all(p == 4.0 for p in pausen)


def test_kommandozeile_ohne_region_und_ohne_erneuern_meldet_fehler(capsys):
    with pytest.raises(SystemExit):
        finden.main([])
    assert "--region" in capsys.readouterr().err


# ===========================================================================
# Greiz und Hanau: die zwei Fehler der ersten Fassung
# ===========================================================================


def test_greiz_ist_in_der_beilage(beilage_echt):
    """Fehlte, weil OSM den Kreis nur mit dem amtlichen Gemeindeschluessel fuehrt -
    eine Firmensuche dort haette stumm nichts gefunden."""
    assert ("Landkreis Greiz", "16076") in [tuple(x) for x in beilage_echt["DE-TH"]] or any(
        "Greiz" in n and r == "16076" for n, r in beilage_echt["DE-TH"]
    )


def test_hanau_ist_kein_kreis(beilage_echt):
    """Stadt mit Sonderstatus im Main-Kinzig-Kreis, kein Kreis."""
    assert not any(r == "06415" or "Hanau" in n for n, r in beilage_echt["DE-HE"])
    assert "06415" in finden.KEIN_KREIS
    assert "Sonderstatus" in finden.KEIN_KREIS["06415"]


def test_beilage_hat_ueberall_fuenfstellige_schluessel(beilage_echt):
    for iso in RS_PRAEFIX:
        for name, rs in beilage_echt.get(iso, []):
            assert len(rs) == 5 and rs.isdigit(), (name, rs)


@pytest.mark.parametrize(
    "tags, erwartet",
    [
        ({"de:regionalschluessel": "040120000000"}, "04012"),
        ({"de:regionalschluessel": "07233"}, "07233"),
        ({"de:amtlicher_gemeindeschluessel": "16076"}, "16076"),
        ({"de:amtlicher_gemeindeschluessel": "16076022"}, "16076"),
        ({"de:regionalschluessel": "04012", "de:amtlicher_gemeindeschluessel": "99999"}, "04012"),
        ({}, ""),
        ({"de:regionalschluessel": "0701"}, ""),       # zu kurz
        ({"de:regionalschluessel": "abcde1"}, ""),     # keine Ziffern
    ],
)
def test_kreisschluessel_aus_den_tags(tags, erwartet):
    assert finden.kreis_schluessel(tags) == erwartet


def test_kreise_live_nimmt_kreise_nur_mit_amtlichem_schluessel_mit(dateien, monkeypatch):
    gesehen = {}

    def falsche_abfrage(query, leise=False, max_sekunden=None):
        gesehen["query"] = query
        return [
            {"tags": {"name": "Greiz", "de:amtlicher_gemeindeschluessel": "16076"}},     # nur AGS
            {"tags": {"name": "Gotha", "de:regionalschluessel": "160670000000"}},         # 12-stellig
            {"tags": {"name": "Hanau", "de:regionalschluessel": "064150000000"}},         # kein Kreis
        ]

    monkeypatch.setattr(finden, "abfragen", falsche_abfrage)
    assert kreise("DE-TH") == [("Gotha", "16067"), ("Greiz", "16076")]
    assert "de:regionalschluessel" in gesehen["query"]
    assert "de:amtlicher_gemeindeschluessel" in gesehen["query"], "beide Tags abfragen"


def test_lesen_normalisiert_aeltere_dateien(dateien):
    """Eine Datei der ersten Fassung (12-stellig, mit Hanau) darf sich nicht anders verhalten."""
    _cache, beilage = dateien
    beilage.write_text(json.dumps({"DE-HE": [["Hanau", "064150000000"], ["Kassel", "066110000000"]]}), encoding="utf-8")
    assert kreise_lokal("DE-HE") == [("Kassel", "06611")]


def test_kreis_filter_ist_fuer_jeden_kreis_der_beilage_gueltig(beilage_echt):
    import re

    for iso in RS_PRAEFIX:
        for _name, rs in beilage_echt.get(iso, []):
            f = finden.kreis_filter(rs)
            assert re.fullmatch(r'\(area\["de:regionalschluessel"~"\^\d{5}"\];area\["de:amtlicher_gemeindeschluessel"~"\^\d{5}"\];\)', f), f
