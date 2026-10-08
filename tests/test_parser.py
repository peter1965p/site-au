"""Tests der Parser und Checks gegen gespeichertes HTML."""

from __future__ import annotations

import struct

import pytest

import audit
from audit import (
    AssetInfo,
    LegalInfo,
    collect_asset_urls,
    copyright_year,
    detect_stack,
    find_legal_links,
    find_phones,
    has_consent_tool,
    image_dimensions,
    normalize_phone,
    normalize_url,
)
from conftest import findings_text, load_fixture, make_context, run_check, severity
from selectolax.lexbor import LexborHTMLParser


# --- URL-Normalisierung ----------------------------------------------------


def test_url_ohne_schema_probiert_erst_https():
    assert normalize_url("beispiel.de") == ["https://beispiel.de", "http://beispiel.de"]


def test_url_mit_schema_bleibt_unveraendert():
    assert normalize_url("http://beispiel.de") == ["http://beispiel.de"]
    assert normalize_url(" https://beispiel.de/pfad ") == ["https://beispiel.de/pfad"]


def test_leere_url_gibt_keine_kandidaten():
    assert normalize_url("   ") == []


# --- Viewport / Mobil ------------------------------------------------------


def test_fehlender_viewport_ist_maximalbefund(baukasten):
    assert severity(baukasten, "mobil") == 1.0
    assert "viewport" in findings_text(baukasten, "mobil")


def test_sauberer_viewport_ist_unauffaellig(modern):
    assert severity(modern, "mobil") == 0.0


def test_gesperrter_zoom_wird_erkannt(wp_alt):
    sev = severity(wp_alt, "mobil")
    assert 0 < sev < 1.0
    assert "zoom" in findings_text(wp_alt, "mobil")


def test_viewport_ohne_device_width_faellt_auf():
    ctx = make_context(html='<html><head><meta name="viewport" content="width=1024"></head><body></body></html>')
    assert severity(ctx, "mobil") > 0
    assert "device-width" in findings_text(ctx, "mobil")


def test_viewport_wird_unabhaengig_von_grossschreibung_gefunden():
    ctx = make_context(html='<html><head><meta NAME="VIEWPORT" content="width=device-width, initial-scale=1"></head><body></body></html>')
    assert severity(ctx, "mobil") == 0.0


# --- Technik-Fingerabdruecke ----------------------------------------------


def test_jimdo_wird_erkannt(baukasten):
    _hits, label = detect_stack(baukasten)
    assert label == "Jimdo"
    assert severity(baukasten, "technologie") >= 0.85


def test_tabellenlayout_wird_erkannt(baukasten):
    assert "tabellenlayout" in findings_text(baukasten, "technologie")


def test_veraltete_tags_werden_erkannt(baukasten):
    assert "font" in findings_text(baukasten, "technologie")


def test_frameset_und_flash_sind_maximalbefund(flash):
    text = findings_text(flash, "technologie")
    assert severity(flash, "technologie") == 1.0
    assert "frameset" in text
    assert "flash" in text


def test_alte_wordpress_version_wird_markiert(wp_alt):
    _hits, label = detect_stack(wp_alt)
    assert label == "WordPress 4.9.8"
    assert "veraltet" in findings_text(wp_alt, "technologie")


def test_slider_revolution_wird_erkannt(wp_alt):
    assert "slider revolution" in findings_text(wp_alt, "technologie")


def test_aktuelles_wordpress_ist_kein_schwerer_befund():
    ctx = make_context(html='<html><head><meta name="generator" content="WordPress 6.7.1"></head><body></body></html>')
    _hits, label = detect_stack(ctx)
    assert label == "WordPress 6.7.1"
    assert severity(ctx, "technologie") <= 0.2


def test_saubere_seite_hat_keinen_technikbefund(modern):
    assert severity(modern, "technologie") == 0.0


# --- Copyright-Jahr --------------------------------------------------------


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("© 2011 Pension Musterhaus", 2011),
        ("&copy; 2019", 2019),
        ("Copyright 2008 - 2014 Firma", 2014),
        ("(c) 2026 Firma", 2026),
        ("COPYRIGHT 2017 Alle Rechte vorbehalten", 2017),
        ("Seit 1998 fuer Sie da", None),
        ("Kein Hinweis hier", None),
    ],
)
def test_copyright_jahr_wird_gelesen(text, erwartet):
    assert copyright_year(text) == erwartet


def test_jahresspanne_nimmt_das_spaetere_jahr():
    assert copyright_year("© 2005-2023 Firma") == 2023


def test_altes_copyright_schlaegt_an(baukasten):
    assert severity(baukasten, "aktualitaet") > 0
    assert "2011" in findings_text(baukasten, "aktualitaet")


def test_aktuelles_copyright_ist_unauffaellig(modern):
    assert severity(modern, "aktualitaet") == 0.0


def test_alter_last_modified_header_schlaegt_an():
    ctx = make_context(
        html="<html><body><p>Hallo</p></body></html>",
        headers={"Last-Modified": "Tue, 15 Nov 2016 12:45:26 GMT"},
    )
    assert severity(ctx, "aktualitaet") > 0
    assert "unveraendert" in findings_text(ctx, "aktualitaet")


def test_kaputter_last_modified_header_kippt_den_check_nicht():
    ctx = make_context(html="<html><body>x</body></html>", headers={"Last-Modified": "voellig kaputt"})
    assert severity(ctx, "aktualitaet") == 0.0


# --- Rechtliches -----------------------------------------------------------


def test_impressum_und_datenschutz_werden_gefunden():
    tree = LexborHTMLParser(load_fixture("modern_sauber.html"))
    imp, dat = find_legal_links(tree, "https://beispiel.de/")
    assert imp == "https://beispiel.de/impressum"
    assert dat == "https://beispiel.de/datenschutz"


def test_fehlendes_impressum_schlaegt_an(baukasten):
    assert severity(baukasten, "recht") > 0
    assert "impressum" in findings_text(baukasten, "recht")


def test_fehlender_datenschutz_schlaegt_an(wp_alt):
    assert "datenschutz" in findings_text(wp_alt, "recht")


def test_google_fonts_direkt_von_google_schlaegt_an(baukasten):
    assert "google fonts" in findings_text(baukasten, "recht")


def test_saubere_seite_hat_keinen_rechtsbefund(modern):
    assert severity(modern, "recht") == 0.0


def test_rechtsbefunde_sind_als_hinweis_formuliert(baukasten):
    assert "geprueft werden" in findings_text(baukasten, "recht")


def test_consent_tool_wird_an_eingebundener_datei_erkannt():
    html = '<html><head><link rel="stylesheet" href="/css/cookie-law-info.css"></head><body></body></html>'
    assert has_consent_tool(LexborHTMLParser(html), html.lower())


def test_consent_tool_wird_am_banner_container_erkannt():
    html = '<html><body><div id="ocn-cookie-wrap">Hinweis</div></body></html>'
    assert has_consent_tool(LexborHTMLParser(html), html.lower())


def test_blosser_footer_link_gilt_nicht_als_consent_tool():
    """Ein Link 'Cookie-Richtlinie' ist kein Banner - sonst uebersehen wir
    genau die Seiten, bei denen der Hinweis fehlt."""
    html = '<html><body><a href="/cookie-richtlinie">Cookie-Richtlinie</a></body></html>'
    assert not has_consent_tool(LexborHTMLParser(html), html.lower())


def test_externe_dienste_ohne_consent_schlagen_an():
    html = """<html><head><script src="https://www.googletagmanager.com/gtag/js"></script></head>
    <body><a href="/impressum">Impressum</a><a href="/datenschutz">Datenschutz</a></body></html>"""
    ctx = make_context(html=html)
    text = findings_text(ctx, "recht")
    assert "consent" in text or "cookie" in text
    assert "geprueft werden" in text


# --- SEO -------------------------------------------------------------------


def test_keyword_stuffing_im_titel_wird_erkannt(baukasten):
    assert "ueberladen" in findings_text(baukasten, "seo")


def test_fehlende_description_schlaegt_an():
    ctx = make_context(html="<html><head><title>Ein ordentlicher Titel</title></head><body><h1>Hallo</h1></body></html>")
    assert "description" in findings_text(ctx, "seo")


def test_fehlender_titel_ist_maximalbefund():
    ctx = make_context(html="<html><head></head><body><h1>Hallo</h1></body></html>")
    assert severity(ctx, "seo") == 1.0


def test_mehrere_h1_schlagen_an(wp_alt):
    assert "h1" in findings_text(wp_alt, "seo")


def test_fehlende_alt_texte_werden_anteilig_gewertet(wp_alt):
    text = findings_text(wp_alt, "seo")
    assert "alt-text" in text


def test_alt_texte_vorhanden_erzeugen_keinen_befund(modern):
    assert "alt-text" not in findings_text(modern, "seo")


def test_saubere_seite_hat_keinen_seo_befund(modern):
    assert severity(modern, "seo") == 0.0


# --- Kontakt ---------------------------------------------------------------


def test_fehlender_tel_link_schlaegt_an(baukasten):
    assert severity(baukasten, "kontakt") > 0
    assert "telefonnummer" in findings_text(baukasten, "kontakt")


def test_tel_link_und_formular_sind_unauffaellig(modern):
    assert severity(modern, "kontakt") == 0.0


def test_weder_telefon_noch_formular_ist_maximalbefund():
    ctx = make_context(html="<html><body><p>Nur Text, keine Kontaktmoeglichkeit.</p></body></html>")
    assert severity(ctx, "kontakt") == 1.0


def test_suchformular_zaehlt_nicht_als_kontaktformular():
    html = '<html><body><form action="/search"><input name="search"></form></body></html>'
    assert severity(make_context(html=html), "kontakt") == 1.0


# --- HTTPS -----------------------------------------------------------------


def test_nur_http_ist_maximalbefund():
    ctx = make_context(html="<html><body>x</body></html>", url="http://beispiel.de/")
    assert severity(ctx, "https") == 1.0
    assert "verschluesselung" in findings_text(ctx, "https")


def test_abgelaufenes_zertifikat_wird_benannt():
    ctx = make_context(html="<html><body>x</body></html>", tls_problem="certificate has expired")
    assert severity(ctx, "https") == 1.0
    assert "abgelaufen" in findings_text(ctx, "https")


def test_mixed_content_wird_erkannt(wp_alt):
    assert "mixed content" in findings_text(wp_alt, "https")


def test_schema_org_angabe_ist_kein_mixed_content():
    """itemtype="http://schema.org/..." ist kein nachgeladener Inhalt."""
    html = '<html><body itemscope itemtype="http://schema.org/LocalBusiness"><a href="http://partner.de">Partner</a></body></html>'
    ctx = make_context(html=html)
    assert "mixed content" not in findings_text(ctx, "https")


def test_saubere_https_seite_ist_unauffaellig(modern):
    assert severity(modern, "https") == 0.0


# --- Erreichbarkeit --------------------------------------------------------


def test_nicht_erreichbare_seite_ist_maximalbefund():
    ctx = make_context(error="Timeout", html="")
    assert severity(ctx, "erreichbarkeit") == 1.0


def test_nicht_erreichbare_seite_setzt_andere_checks_aus():
    ctx = make_context(error="Timeout", html="")
    for key in ("https", "mobil", "seo", "recht", "technologie", "kontakt"):
        assert severity(ctx, key) is None, f"{key} darf ohne Seite nicht werten"


def test_nicht_erreichbare_seite_erreicht_den_hoechsten_score():
    ctx = make_context(error="Timeout", html="")
    assert audit.score_from_checks([c(ctx) for c in audit.CHECKS]) == 100.0


def test_serverfehler_schlaegt_an():
    ctx = make_context(html="<html><body>Fehler</body></html>", status=503)
    assert severity(ctx, "erreichbarkeit") > 0.8


def test_lange_redirect_kette_schlaegt_an():
    ctx = make_context(html="<html><body>x</body></html>", redirects=4)
    assert severity(ctx, "erreichbarkeit") > 0
    assert "weiterleitung" in findings_text(ctx, "erreichbarkeit")


def test_kurze_redirect_kette_ist_in_ordnung():
    ctx = make_context(html="<html><body>x</body></html>", redirects=2)
    assert severity(ctx, "erreichbarkeit") == 0.0


# --- Telefonnummern --------------------------------------------------------


@pytest.mark.parametrize(
    "roh, erwartet",
    [
        ("06591 123456", "496591123456"),
        ("+49 6591 123456", "496591123456"),
        ("0049 6591 123456", "496591123456"),
        ("06591/123456", "496591123456"),
        ("06591-123 456", "496591123456"),
        ("123", None),
        ("2026", None),
    ],
)
def test_telefonnummern_werden_normalisiert(roh, erwartet):
    assert normalize_phone(roh) == erwartet


def test_gleiche_nummer_in_zwei_schreibweisen_ist_dieselbe():
    assert normalize_phone("06591 123456") == normalize_phone("+49 (0)6591 / 12 34 56")


def test_nummern_werden_im_text_gefunden():
    gefunden = find_phones("Rufen Sie an: 06591 123456 oder faxen Sie an 06591 123457.")
    assert "496591123456" in gefunden
    assert len(gefunden) == 2


def test_abweichende_nummer_in_der_description_schlaegt_an(wp_alt):
    # Fixture: Description nennt 02831 998877, der Text nennt 06591 445566.
    assert severity(wp_alt, "konsistenz") == 1.0


def test_gleiche_nummer_in_description_und_text_ist_unauffaellig(modern):
    assert severity(modern, "konsistenz") == 0.0


def test_description_ohne_nummer_erzeugt_keinen_befund():
    ctx = make_context(html='<html><head><meta name="description" content="Ohne Nummer"></head><body>Text</body></html>')
    assert severity(ctx, "konsistenz") == 0.0


# --- Bildmasse -------------------------------------------------------------


def _png(w: int, h: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h)


def _gif(w: int, h: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 8


def _jpeg(w: int, h: int) -> bytes:
    return b"\xff\xd8" + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", h, w) + b"\x00" * 8


def _webp_vp8x(w: int, h: int) -> bytes:
    body = b"WEBP" + b"VP8X" + b"\x00" * 8 + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize(
    "daten, erwartet",
    [
        (_png(1200, 800), (1200, 800)),
        (_gif(88, 31), (88, 31)),
        (_jpeg(640, 480), (640, 480)),
        (_webp_vp8x(1920, 1080), (1920, 1080)),
        (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 120"></svg>' + b" " * 40, (240, 120)),
        (b'<svg width="64" height="64" xmlns="http://www.w3.org/2000/svg"></svg>' + b" " * 40, (64, 64)),
    ],
)
def test_bildmasse_werden_aus_dem_header_gelesen(daten, erwartet):
    assert image_dimensions(daten) == erwartet


def test_unbekanntes_format_gibt_nichts_zurueck():
    assert image_dimensions(b"das ist kein bild, sondern nur text" * 3) is None


def test_zu_kurze_daten_kippen_nicht():
    assert image_dimensions(b"\x89PNG") is None
    assert image_dimensions(b"") is None


# --- Bilder-Check ----------------------------------------------------------


def test_riesenbild_schlaegt_an(modern):
    modern.assets = AssetInfo(refs=3, measured=3, total_bytes=4_000_000, oversized=[("/haus.jpg", 2_400_000)])
    text = findings_text(modern, "bilder")
    assert severity(modern, "bilder") > 0.8
    assert "1,5 mb" in text


def test_hoher_anteil_kleiner_bilder_schlaegt_an(modern):
    modern.assets = AssetInfo(refs=5, measured=5, total_bytes=200_000, images_measured=5, images_small=4)
    assert "schmaler als 400" in findings_text(modern, "bilder")


def test_schlanke_seite_ist_unauffaellig(modern):
    modern.assets = AssetInfo(refs=4, measured=4, total_bytes=300_000, images_measured=4, images_small=0)
    assert severity(modern, "bilder") == 0.0


def test_ohne_vermessene_assets_wird_nicht_gewertet(modern):
    modern.assets = None
    assert severity(modern, "bilder") is None


def test_wenige_bilder_erzeugen_keine_anteilsaussage(modern):
    # Bei zwei Bildern ist ein Anteil nicht aussagekraeftig.
    modern.assets = AssetInfo(refs=2, measured=2, total_bytes=100_000, images_measured=2, images_small=2)
    assert "schmaler als 400" not in findings_text(modern, "bilder")


# --- Asset-Sammlung --------------------------------------------------------


def test_assets_werden_absolut_aufgeloest_und_begrenzt():
    tree = LexborHTMLParser(load_fixture("modern_sauber.html"))
    bilder, sonstige, gesamt = collect_asset_urls(tree, "https://beispiel.de/", limit=10)
    assert "https://beispiel.de/haus.jpg" in bilder
    assert any(u.endswith("app.css") for u in sonstige)
    assert gesamt == len(bilder) + len(sonstige)


def test_bilder_haben_vorrang_vor_css_und_js():
    html = """<html><head><link rel="stylesheet" href="/a.css"><script src="/b.js"></script></head>
    <body><img src="/1.jpg"><img src="/2.jpg"></body></html>"""
    bilder, sonstige, _ = collect_asset_urls(LexborHTMLParser(html), "https://beispiel.de/", limit=2)
    assert len(bilder) == 2
    assert sonstige == []


def test_data_uris_werden_uebersprungen():
    html = '<html><body><img src="data:image/png;base64,iVBOR"><img src="/echt.png"></body></html>'
    bilder, _s, _g = collect_asset_urls(LexborHTMLParser(html), "https://beispiel.de/", limit=10)
    assert bilder == ["https://beispiel.de/echt.png"]


def test_assets_werden_nicht_doppelt_gezaehlt():
    html = '<html><body><img src="/gleich.png"><img src="/gleich.png"></body></html>'
    bilder, _s, _g = collect_asset_urls(LexborHTMLParser(html), "https://beispiel.de/", limit=10)
    assert len(bilder) == 1


# --- PageSpeed -------------------------------------------------------------


def test_pagespeed_ohne_lauf_wird_nicht_gewertet(modern):
    assert severity(modern, "pagespeed") is None


def test_schlechter_pagespeed_schlaegt_an():
    ctx = make_context("modern_sauber.html", psi_score=18)
    assert severity(ctx, "pagespeed") == pytest.approx(0.82)
    assert "18/100" in findings_text(ctx, "pagespeed")


def test_guter_pagespeed_ist_unauffaellig():
    ctx = make_context("modern_sauber.html", psi_score=96)
    assert severity(ctx, "pagespeed") == pytest.approx(0.04)


# --- Gesamtbild ------------------------------------------------------------


def test_baukasten_seite_scort_deutlich_schlechter_als_saubere_seite(baukasten, modern):
    """Das Kernversprechen des Tools: die schlechteren Seiten stehen oben."""
    schlecht = audit.score_from_checks([c(baukasten) for c in audit.CHECKS])
    gut = audit.score_from_checks([c(modern) for c in audit.CHECKS])
    assert schlecht > gut + 30


def test_saubere_seite_bleibt_im_gruenen_bereich(modern):
    assert audit.score_from_checks([c(modern) for c in audit.CHECKS]) < 10


def test_kein_check_kippt_bei_leerem_html():
    ctx = make_context(html="<html></html>")
    for check in audit.CHECKS:
        check(ctx)


def test_kein_check_kippt_bei_kaputtem_html():
    ctx = make_context(html="<html><body><p>offen <div><img <<< </body>")
    for check in audit.CHECKS:
        check(ctx)
