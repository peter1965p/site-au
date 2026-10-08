"""Tests der Kontaktsuche in Branchenverzeichnissen - ohne Netzzugriff."""

from __future__ import annotations

import pytest

from kontaktsuche import (
    MIN_AEHNLICHKEIT,
    Kontakttreffer,
    namen_passen,
    oertliche_auswerten,
    oertliche_url,
    ort_passt,
    such_links,
)


# --- Namensabgleich --------------------------------------------------------


def test_gleicher_name_passt():
    assert namen_passen("Gans Haustechnik", "Gans Haustechnik GmbH & Co. KG") > 0.8


def test_firmenname_im_laengeren_eintrag_wird_erkannt():
    """"Peter Mueller" steckt in "Fleischer-Fachgeschaeft Peter Mueller GmbH"."""
    assert namen_passen("Peter Müller", "Fleischer-Fachgeschäft Peter Müller GmbH Metzgerei") >= MIN_AEHNLICHKEIT


def test_umlautschreibweise_ist_egal():
    assert namen_passen("Peter Mueller", "Peter Müller GmbH") >= MIN_AEHNLICHKEIT


def test_fremde_firma_passt_nicht():
    assert namen_passen("Gans Haustechnik", "Bäckerei Schmitz") < MIN_AEHNLICHKEIT


def test_rechtsform_allein_reicht_nicht():
    """Sonst passt jede GmbH zu jeder anderen."""
    assert namen_passen("Schreinerei Schneiders GmbH", "Autohaus Weber GmbH") < MIN_AEHNLICHKEIT


# --- Ortsabgleich ----------------------------------------------------------


def test_ort_in_der_adresse_passt():
    assert ort_passt("Müllenborn", "Müllenborner Str. 62 54568 Gerolstein, Müllenborn")


def test_ort_mit_umlautvariante_passt():
    assert ort_passt("Muellenborn", "54568 Gerolstein, Müllenborn")
    assert ort_passt("Müllenborn", "54568 Gerolstein, Muellenborn")


def test_falscher_ort_passt_nicht():
    """Der entscheidende Schutz: sonst landet die Nummer eines wildfremden
    Betriebs in der Liste und jemand waehlt sie."""
    assert not ort_passt("Müllenborn", "Durlacher Str. 41 76229 Karlsruhe")


def test_ohne_adresse_kein_treffer():
    assert not ort_passt("Müllenborn", "")


def test_ohne_gesuchten_ort_wird_nicht_geprueft():
    assert ort_passt("", "irgendeine Adresse")


def test_ortsteil_wird_gefunden():
    assert ort_passt("Bad Neuenahr-Ahrweiler", "Hauptstr. 1, 53474 Bad Neuenahr")


# --- Auswertung der Trefferliste ------------------------------------------


TREFFER_HTML = """
<html><body>
<div class="hit">
  <h2>Fleischer-Fachgeschäft Peter Müller GmbH Metzgerei</h2>
  <div class="phoneblock">Tel. 06591 9 54 40 Gratis anrufen!</div>
  <address>Müllenborner Str. 62 54568 Gerolstein, Müllenborn</address>
</div>
<div class="hit">
  <h2>Müller-Diercksen Elke u. Peter</h2>
  <div class="phoneblock">Tel. 0721 48 29 45</div>
  <address>Durlacher Str. 41 76229 Karlsruhe</address>
</div>
</body></html>
"""


def test_richtiger_treffer_wird_gewaehlt():
    t = oertliche_auswerten(TREFFER_HTML, "Peter Müller", "Müllenborn")
    assert t is not None
    assert t.telefon == "06591 9 54 40"
    assert "Müllenborn" in t.adresse


def test_treffer_am_falschen_ort_wird_verworfen():
    """Ohne Ortsabgleich liefert ein haeufiger Name irgendeine Nummer."""
    assert oertliche_auswerten(TREFFER_HTML, "Peter Müller", "Daun") is None


def test_fremder_name_liefert_nichts():
    assert oertliche_auswerten(TREFFER_HTML, "Autohaus Weber", "Müllenborn") is None


def test_treffer_ohne_telefon_zaehlt_nicht():
    html = """<div class="hit"><h2>Gans Haustechnik</h2>
              <address>54584 Jünkerath</address></div>"""
    assert oertliche_auswerten(html, "Gans Haustechnik", "Jünkerath") is None


def test_leeres_oder_kaputtes_html_kippt_nicht():
    assert oertliche_auswerten("", "Firma", "Ort") is None
    assert oertliche_auswerten("<html><body><div class=hit>", "Firma", "Ort") is None


def test_telefonnummer_wird_von_beiwerk_befreit():
    t = oertliche_auswerten(TREFFER_HTML, "Peter Müller", "Müllenborn")
    assert "Gratis" not in t.telefon
    assert "Tel" not in t.telefon


# --- Links zum Selbernachschlagen -----------------------------------------


def test_such_links_enthalten_die_verzeichnisse():
    titel = [t for t, _u in such_links("Gans Haustechnik", "Jünkerath")]
    assert {"Das Örtliche", "Gelbe Seiten", "11880", "Google"} <= set(titel)


def test_such_links_sind_kodiert():
    for _titel, adresse in such_links("Müller & Sohn", "Bad Neuenahr"):
        assert " " not in adresse
        assert adresse.startswith("https://")


def test_impressum_link_nur_mit_website():
    ohne = [t for t, _u in such_links("Firma", "Ort")]
    mit = [t for t, _u in such_links("Firma", "Ort", "https://firma.de/")]
    assert "Impressum über Google" not in ohne
    assert "Impressum über Google" in mit


def test_oertliche_url_enthaelt_name_und_ort():
    u = oertliche_url("Gans Haustechnik", "Jünkerath")
    assert "Gans" in u and "nkerath" in u
