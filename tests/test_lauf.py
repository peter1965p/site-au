"""Tests der Infrastruktur: Rate-Limit, Cache, CSV-Einlesen, Ausgabe."""

from __future__ import annotations

import asyncio
import csv
import time

import pytest

import audit
from audit import Cache, HostLimiter, Lead, Result, read_leads, write_csv, write_html


# --- Rate-Limit ------------------------------------------------------------


async def test_limiter_haelt_abstand_pro_host():
    limiter = HostLimiter(interval=0.25)
    start = time.monotonic()
    for _ in range(3):
        await limiter.wait("beispiel.de")
    dauer = time.monotonic() - start
    assert dauer >= 0.5, "drei Requests brauchen mindestens zwei Pausen"


async def test_limiter_bremst_verschiedene_hosts_nicht_gegenseitig():
    limiter = HostLimiter(interval=0.25)
    start = time.monotonic()
    await asyncio.gather(*(limiter.wait(f"host{i}.de") for i in range(5)))
    assert time.monotonic() - start < 0.2, "fremde Hosts duerfen nicht warten"


async def test_limiter_serialisiert_parallele_zugriffe_auf_denselben_host():
    limiter = HostLimiter(interval=0.2)
    start = time.monotonic()
    await asyncio.gather(*(limiter.wait("beispiel.de") for _ in range(4)))
    assert time.monotonic() - start >= 0.6


# --- Cache -----------------------------------------------------------------


def _result(url: str = "beispiel.de", score: float = 42.0) -> Result:
    return Result(
        name="Beispiel", url=url, ort="Daun", branche="Handwerk",
        final_url=f"https://{url}/", status="ok", score=score, cms="Jimdo",
        argumente=["Ein Argument"], befunde=["[Mobil] Kein Viewport"],
        severities={"mobil": 1.0}, geprueft_am="2026-10-08T12:00:00",
    )


def test_cache_legt_ab_und_liest_zurueck(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    lead = Lead("Beispiel", "beispiel.de", "Daun", "Handwerk")
    assert cache.get(lead, 0, False) is None
    cache.put(_result(), False)
    wieder = cache.get(lead, 0, False)
    assert wieder is not None
    assert wieder.score == 42.0
    assert wieder.befunde == ["[Mobil] Kein Viewport"]
    cache.close()


def test_cache_ignoriert_schema_und_schraegstrich(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    cache.put(_result(url="beispiel.de"), False)
    assert cache.get(Lead("B", "Beispiel.DE/", "", ""), 0, False) is not None
    cache.close()


def test_cache_uebernimmt_geaenderte_stammdaten(tmp_path):
    """Ort und Branche kommen aus der CSV, nicht aus dem Cache - sonst
    schleppt ein alter Eintrag veraltete Stammdaten mit."""
    cache = Cache(tmp_path / "c.sqlite")
    cache.put(_result(), False)
    wieder = cache.get(Lead("Neuer Name", "beispiel.de", "Gerolstein", "Gastronomie"), 0, False)
    assert wieder.name == "Neuer Name"
    assert wieder.ort == "Gerolstein"
    assert wieder.branche == "Gastronomie"
    assert wieder.score == 42.0, "die Bewertung selbst bleibt aus dem Cache"
    cache.close()


def test_cache_liefert_eintrag_ohne_pagespeed_nicht_fuer_pagespeed_lauf(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    cache.put(_result(), with_pagespeed=False)
    lead = Lead("Beispiel", "beispiel.de", "", "")
    assert cache.get(lead, 0, want_pagespeed=True) is None
    assert cache.get(lead, 0, want_pagespeed=False) is not None
    cache.close()


def test_cache_verfaellt_nach_max_age(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    alt = _result()
    alt.geprueft_am = "2020-01-01T00:00:00"
    cache.put(alt, False)
    lead = Lead("Beispiel", "beispiel.de", "", "")
    assert cache.get(lead, max_age_days=7, want_pagespeed=False) is None
    assert cache.get(lead, max_age_days=0, want_pagespeed=False) is not None, "0 = nie verfallen"
    cache.close()


def test_cache_ueberschreibt_bei_erneutem_lauf(tmp_path):
    cache = Cache(tmp_path / "c.sqlite")
    cache.put(_result(score=10.0), False)
    cache.put(_result(score=90.0), False)
    assert cache.get(Lead("B", "beispiel.de", "", ""), 0, False).score == 90.0
    cache.close()


# --- CSV einlesen ----------------------------------------------------------


def test_csv_wird_eingelesen(tmp_path):
    p = tmp_path / "leads.csv"
    p.write_text("name,url,ort,branche\nFirma A,a.de,Daun,Handwerk\n", encoding="utf-8")
    leads = read_leads(p)
    assert leads == [Lead("Firma A", "a.de", "Daun", "Handwerk")]


def test_csv_mit_semikolon_wird_erkannt(tmp_path):
    p = tmp_path / "leads.csv"
    p.write_text("name;url;ort;branche\nFirma A;a.de;Daun;Handwerk\n", encoding="utf-8")
    assert read_leads(p)[0].url == "a.de"


def test_csv_spalten_sind_gross_klein_egal(tmp_path):
    p = tmp_path / "leads.csv"
    p.write_text("Name,URL,Ort,Branche\nFirma A,a.de,Daun,Handwerk\n", encoding="utf-8")
    assert read_leads(p)[0].ort == "Daun"


def test_csv_ohne_optionale_spalten_geht_auch(tmp_path):
    p = tmp_path / "leads.csv"
    p.write_text("url\na.de\nb.de\n", encoding="utf-8")
    leads = read_leads(p)
    assert [l.url for l in leads] == ["a.de", "b.de"]
    assert leads[0].name == "a.de", "ohne Name faellt das Tool auf die URL zurueck"


def test_csv_ohne_url_spalte_bricht_verstaendlich_ab(tmp_path):
    p = tmp_path / "leads.csv"
    p.write_text("name,ort\nFirma A,Daun\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="url"):
        read_leads(p)


def test_leere_zeilen_werden_uebersprungen(tmp_path):
    p = tmp_path / "leads.csv"
    p.write_text("name,url\nFirma A,a.de\nFirma B,\n", encoding="utf-8")
    assert len(read_leads(p)) == 1


def test_bom_am_dateianfang_stoert_nicht(tmp_path):
    p = tmp_path / "leads.csv"
    p.write_text("﻿name,url\nFirma A,a.de\n", encoding="utf-8")
    assert read_leads(p)[0].name == "Firma A"


# --- Ausgabe ---------------------------------------------------------------


def test_csv_ausgabe_enthaelt_argumente_und_severities(tmp_path):
    ziel = tmp_path / "ergebnis.csv"
    write_csv([_result()], ziel)
    zeilen = list(csv.DictReader(ziel.open(encoding="utf-8"), delimiter=";"))
    assert zeilen[0]["score"] == "42.0"
    assert zeilen[0]["argumente"] == "Ein Argument"
    assert zeilen[0]["s_mobil"] == "1.00"
    assert zeilen[0]["s_https"] == "", "nicht gelaufene Checks bleiben leer"


def test_html_ausgabe_ist_eine_einzelne_datei(tmp_path):
    ziel = tmp_path / "ergebnis.html"
    write_html([_result()], ziel, "leads.csv")
    html = ziel.read_text(encoding="utf-8")
    assert "<!doctype html>" in html.lower()
    assert "Beispiel" in html
    assert "src=\"http" not in html, "keine externen Skripte, die Datei muss allein laufen"
    assert "cdn." not in html


def test_html_ausgabe_maskiert_sonderzeichen(tmp_path):
    """Firmennamen wie 'Meier & Sohn <GmbH>' duerfen das Markup nicht zerlegen."""
    r = _result()
    r.name = "Meier & Sohn <GmbH>"
    r.befunde = ["<script>alert(1)</script>"]
    ziel = tmp_path / "ergebnis.html"
    write_html([r], ziel, "leads.csv")
    html = ziel.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "Meier &amp; Sohn" in html or "Meier \\u0026 Sohn" in html


def test_html_enthaelt_die_gewichte_des_laufs(tmp_path):
    ziel = tmp_path / "ergebnis.html"
    write_html([_result()], ziel, "leads.csv")
    html = ziel.read_text(encoding="utf-8")
    for label in ("Mobil", "Rechtliches", "Technik"):
        assert label in html


# --- Argumentparser --------------------------------------------------------


def test_standardaufruf_setzt_sinnvolle_vorgaben():
    args = audit.build_parser().parse_args(["leads.csv"])
    assert str(args.out) == "ergebnis"
    assert args.pagespeed is False
    assert args.refresh is False
    assert args.concurrency == 10
    assert args.host_interval == 1.0


def test_aufruf_aus_der_spezifikation_wird_akzeptiert():
    args = audit.build_parser().parse_args(["leads.csv", "--out", "ergebnis"])
    assert str(args.csv) == "leads.csv"
    assert str(args.out) == "ergebnis"
