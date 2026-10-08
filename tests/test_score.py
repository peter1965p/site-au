"""Tests der Score-Logik: Kombination, Normierung, Argumente."""

from __future__ import annotations

import pytest

import audit
from audit import CheckResult, Finding, build_arguments, combine, score_from_checks
from conftest import make_context


# --- combine ---------------------------------------------------------------


def test_combine_ohne_signale_ist_null():
    assert combine([]) == 0.0
    assert combine([0.0, 0.0]) == 0.0


def test_combine_einzelsignal_bleibt_unveraendert():
    assert combine([0.6]) == 0.6


def test_combine_staerkstes_signal_dominiert():
    # Das schwerste Signal gibt den Ton an, kleine Zusaetze heben nur leicht an.
    assert combine([0.8, 0.2]) == pytest.approx(0.85)
    assert combine([0.2, 0.8]) == pytest.approx(0.85), "Reihenfolge darf nichts aendern"


def test_combine_haeufung_kleiner_maengel_kommt_nach_oben():
    einzeln = combine([0.3])
    gehaeuft = combine([0.3, 0.3, 0.3, 0.3])
    assert gehaeuft > einzeln
    assert gehaeuft == pytest.approx(0.525)


def test_combine_deckelt_bei_eins():
    assert combine([1.0, 1.0, 1.0, 1.0]) == 1.0
    assert combine([0.9, 0.9, 0.9]) == 1.0


def test_combine_ignoriert_nullsignale():
    assert combine([0.5, 0.0, 0.0]) == 0.5


# --- score_from_checks -----------------------------------------------------


WEIGHTS_TEST = {"a": 10.0, "b": 30.0, "c": 60.0}


def test_score_ohne_maengel_ist_null():
    results = [CheckResult(k, 0.0) for k in WEIGHTS_TEST]
    assert score_from_checks(results, WEIGHTS_TEST) == 0.0


def test_score_gewichtet_nach_wichtigkeit():
    # Nur der schwerste Check faellt durch.
    nur_c = score_from_checks(
        [CheckResult("a", 0.0), CheckResult("b", 0.0), CheckResult("c", 1.0)], WEIGHTS_TEST
    )
    nur_a = score_from_checks(
        [CheckResult("a", 1.0), CheckResult("b", 0.0), CheckResult("c", 0.0)], WEIGHTS_TEST
    )
    assert nur_c > nur_a


def test_score_wird_bei_hundert_gedeckelt():
    results = [CheckResult(k, 1.0) for k in WEIGHTS_TEST]
    assert score_from_checks(results, WEIGHTS_TEST) == 100.0


def test_uebersprungene_checks_verschieben_die_skala_nicht():
    """Ein Check ohne Ergebnis darf den Score nicht verwaessern.

    Faellt ein Check mit Severity None aus, schrumpft die Bezugsgroesse im
    gleichen Verhaeltnis mit. Sonst wuerde `--pagespeed` allein schon jeden
    Score verschieben.
    """
    mit = score_from_checks(
        [CheckResult("a", 0.5), CheckResult("b", 0.5), CheckResult("c", 0.5)], WEIGHTS_TEST
    )
    ohne = score_from_checks(
        [CheckResult("a", 0.5), CheckResult("b", 0.5), CheckResult("c", None)], WEIGHTS_TEST
    )
    assert mit == pytest.approx(ohne)


def test_score_ohne_ein_einziges_ergebnis_ist_null():
    assert score_from_checks([CheckResult("a", None)], WEIGHTS_TEST) == 0.0
    assert score_from_checks([], WEIGHTS_TEST) == 0.0


def test_score_steigt_monoton_mit_der_severity():
    werte = [
        score_from_checks([CheckResult("c", s)], WEIGHTS_TEST) for s in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    assert werte == sorted(werte), "ein schlechterer Befund darf den Score nie senken"
    # Unterhalb der Deckelung muss jede Stufe unterscheidbar bleiben.
    unter_hundert = [w for w in werte if w < 100.0]
    assert len(set(unter_hundert)) == len(unter_hundert)


def test_echte_gewichte_erlauben_die_volle_skala():
    """Mit den echten Gewichten muss ein Totalschaden die 100 erreichen koennen
    und eine saubere Seite bei 0 landen - sonst ist die Skala unbrauchbar."""
    alles_kaputt = [CheckResult(k, 1.0) for k in audit.WEIGHTS]
    alles_gut = [CheckResult(k, 0.0) for k in audit.WEIGHTS]
    assert score_from_checks(alles_kaputt) == 100.0
    assert score_from_checks(alles_gut) == 0.0


def test_realistisches_maengelbild_landet_im_roten_bereich():
    """Viewport fehlt, Baukasten, SEO kaputt - das muss deutlich ueber der
    gelben Schwelle von 35 landen, sonst sortiert das Tool stumpf."""
    results = [
        CheckResult("erreichbarkeit", 0.0),
        CheckResult("https", 0.0),
        CheckResult("mobil", 1.0),
        CheckResult("aktualitaet", 0.0),
        CheckResult("technologie", 0.85),
        CheckResult("recht", 0.0),
        CheckResult("seo", 1.0),
        CheckResult("bilder", 0.5),
        CheckResult("kontakt", 0.5),
        CheckResult("konsistenz", 0.0),
        CheckResult("pagespeed", None),
    ]
    assert score_from_checks(results) > 60


# --- build_arguments -------------------------------------------------------


def test_argumente_hoechstens_vier():
    results = [
        CheckResult(key, 1.0, [Finding(f"Befund {key}", f"Argument {key}")])
        for key in list(audit.WEIGHTS)[:8]
    ]
    assert len(build_arguments(results)) == 4


def test_argumente_nach_gewicht_sortiert():
    results = [
        CheckResult("kontakt", 1.0, [Finding("x", "leicht")]),
        CheckResult("mobil", 1.0, [Finding("y", "schwer")]),
    ]
    assert build_arguments(results)[0] == "schwer", "mobil wiegt mehr als kontakt"


def test_befunde_ohne_argument_tauchen_nicht_auf():
    results = [CheckResult("mobil", 1.0, [Finding("nur technischer Befund", None)])]
    assert build_arguments(results) == []


def test_argumente_ohne_maengel_sind_leer():
    results = [CheckResult("mobil", 0.0, [Finding("a", "b")])]
    assert build_arguments(results) == []


def test_argumente_sind_eindeutig():
    doppelt = "derselbe Satz"
    results = [
        CheckResult("mobil", 1.0, [Finding("a", doppelt)]),
        CheckResult("seo", 1.0, [Finding("b", doppelt)]),
    ]
    assert build_arguments(results) == [doppelt]


def test_rechtliche_argumente_bleiben_hinweise():
    """Rechtliche Befunde duerfen nie als Rechtsbehauptung formuliert sein."""
    ctx = make_context(
        html="""<html><head><meta name="viewport" content="width=device-width">
        <link rel="stylesheet" href="https://fonts.googleapis.com/css?family=Lato">
        <title>Test</title></head><body><h1>Hallo</h1></body></html>"""
    )
    text = " ".join(
        f.text + " " + (f.argument or "") for f in audit.check_recht(ctx).findings
    ).lower()
    assert "geprueft werden" in text
    for behauptung in ("rechtswidrig", "verstoesst", "verstoss gegen", "illegal", "bussgeld", "abmahnung"):
        assert behauptung not in text, f"{behauptung!r} ist eine Rechtsbehauptung"


# --- Gewichte als Konfiguration -------------------------------------------


def test_jeder_check_hat_ein_gewicht():
    ctx = make_context("modern_sauber.html")
    for check in audit.CHECKS:
        assert check(ctx).key in audit.WEIGHTS


def test_jedes_gewicht_hat_ein_label():
    for key in audit.WEIGHTS:
        assert key in audit.LABELS


def test_gewichte_sind_positiv():
    assert all(w > 0 for w in audit.WEIGHTS.values())
