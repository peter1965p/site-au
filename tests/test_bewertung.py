"""Tests zu Slider Revolution, Lazy-Load-Bildern und dem Status "nicht erreichbar"."""

from __future__ import annotations

import argparse
import csv

import pytest
from selectolax.lexbor import LexborHTMLParser

import audit
from audit import (
    SLIDERREV_AKTUELLE_HAUPT,
    STATUS_OK,
    STATUS_PRUEFFEHLER,
    STATUS_UNERREICHBAR,
    AssetInfo,
    BildRef,
    Fetched,
    Lead,
    Result,
    bild_refs,
    bildquelle,
    build_arguments,
    ist_inhaltsbild,
    ist_platzhalter,
    slider_revolution_version,
    srcset_kandidaten,
    zaehle_inhaltsbilder,
)
from conftest import findings_text, load_fixture, make_context, run_check, severity


def _baum(html: str) -> LexborHTMLParser:
    return LexborHTMLParser(html)


def _img(html: str):
    return _baum(f"<html><body><main>{html}</main></body></html>").css_first("img")


# ===========================================================================
# 1. Slider Revolution: nur mit Versionsnachweis ein Sicherheitsargument
# ===========================================================================


def _seite(kopf: str) -> str:
    return f"<html><head>{kopf}</head><body><p>Text</p></body></html>"


def test_version_aus_dem_generator_meta():
    html = _seite('<meta name="generator" content="Powered by Slider Revolution 6.7.38 - responsive Slider">')
    sr = slider_revolution_version(_baum(html), html)
    assert (sr.text, sr.haupt, sr.quelle) == ("6.7.38", 6, "Generator-Meta")


def test_slider_generator_ist_nicht_der_erste_generator():
    """Haus Anny hat drei generator-Tags, der Slider steht an dritter Stelle."""
    html = _seite(
        '<meta name="generator" content="WordPress 6.9.4">'
        '<meta name="generator" content="Powered by WPBakery Page Builder">'
        '<meta name="generator" content="Powered by Slider Revolution 6.7.38 - Slider">'
    )
    assert slider_revolution_version(_baum(html), html).text == "6.7.38"


def test_version_aus_dem_versionsparameter_der_plugin_datei():
    html = _seite('<script src="/wp-content/plugins/revslider/public/assets/js/rs6.min.js?ver=6.4.11"></script>')
    sr = slider_revolution_version(_baum(html), html)
    assert (sr.text, sr.quelle) == ("6.4.11", "Asset-Parameter")


def test_version_parameter_fremder_dateien_zaehlt_nicht():
    """jquery.js?ver= ist die WordPress-Version, nicht die des Sliders."""
    html = _seite(
        '<script src="/wp-includes/js/jquery/jquery.js?ver=5.1.0"></script>'
        '<link rel="stylesheet" href="/wp-content/plugins/revslider/public/assets/css/settings.css">'
    )
    assert slider_revolution_version(_baum(html), html) is None


def test_version_aus_dem_kommentar():
    html = _seite("") + "<!-- START REVOLUTION SLIDER 5.4.8.3 fullwidth mode -->"
    sr = slider_revolution_version(_baum(html), html)
    assert (sr.text, sr.haupt, sr.quelle) == ("5.4.8.3", 5, "Kommentar")


def test_hauptversion_aus_dem_dateipfad():
    html = _seite('<script src="/wp-content/plugins/revslider/sr6/assets/js/rs6.min.js"></script>')
    sr = slider_revolution_version(_baum(html), html)
    assert (sr.haupt, sr.quelle) == (6, "Dateipfad")


def test_ordner_rs_plugin_bedeutet_alte_reihe():
    html = _seite('<script src="/wp-content/plugins/revslider/rs-plugin/js/jquery.themepunch.revolution.min.js"></script>')
    sr = slider_revolution_version(_baum(html), html)
    assert sr.haupt == 5 and sr.quelle == "Dateipfad"


def test_ohne_jeden_hinweis_keine_version():
    html = _seite('<link rel="stylesheet" href="/wp-content/plugins/revslider/public/css/settings.css">')
    assert slider_revolution_version(_baum(html), html) is None


def _technik(html: str) -> audit.CheckResult:
    return run_check(make_context(html=html), "technologie")


def test_aktuelle_hauptversion_gibt_kein_sicherheitsargument():
    """Haus Anny: 6.7.38 liegt in der aktuellen 6.x-Reihe."""
    html = _seite('<meta name="generator" content="Powered by Slider Revolution 6.7.38 - Slider">')
    erg = _technik(html)
    slider = [f for f in erg.findings if f.text.startswith("Slider Revolution")]
    assert slider and slider[0].text == "Slider Revolution 6.7.38"
    assert slider[0].argument is None
    assert build_arguments([erg]) == []


def test_veraltete_hauptversion_bekommt_ein_vorsichtiges_argument():
    html = _seite('<meta name="generator" content="Powered by Slider Revolution 5.4.8.3 - Slider">')
    erg = _technik(html)
    slider = next(f for f in erg.findings if f.text.startswith("Slider Revolution"))
    assert "veraltet" in slider.text
    assert "5.4.8.3" in slider.argument
    assert "geprueft werden" in slider.argument, "Hinweis, keine Behauptung"
    assert any("Slider-Revolution" in a for a in build_arguments([erg]))


def test_reiner_fingerabdruck_bleibt_befund_ohne_argument(wp_alt):
    """Die Fixture hat das Plugin im Quelltext, aber keinen Versionshinweis."""
    erg = run_check(wp_alt, "technologie")
    slider = [f for f in erg.findings if f.text.startswith("Slider Revolution")]
    assert slider, "bleibt als Befund [Technik] stehen"
    assert "nicht ermittelbar" in slider[0].text
    assert slider[0].argument is None, "ohne Version kein Sicherheitsargument"
    assert not any("Slider" in a for a in build_arguments([erg]))


def test_schweregrad_folgt_dem_versionsnachweis():
    def sev(kopf: str) -> float:
        return _technik(_seite(kopf)).severity

    unbekannt = sev('<link href="/wp-content/plugins/revslider/public/css/s.css">')
    aktuell = sev('<meta name="generator" content="Powered by Slider Revolution 6.7.38 - x">')
    veraltet = sev('<meta name="generator" content="Powered by Slider Revolution 5.4.8 - x">')
    assert veraltet > unbekannt > aktuell > 0, (veraltet, unbekannt, aktuell)


def test_aktuelle_hauptversion_ist_eine_einstellbare_konstante():
    assert isinstance(SLIDERREV_AKTUELLE_HAUPT, int) and SLIDERREV_AKTUELLE_HAUPT >= 6


# ===========================================================================
# 2. Bilder: Lazy-Load-Platzhalter, srcset, Inhaltsbilder
# ===========================================================================


@pytest.mark.parametrize(
    "url",
    [
        "data:image/svg+xml,%3Csvg%3E%3C/svg%3E",
        "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==",
        "/wp-content/plugins/a3-lazy-load/assets/images/lazy_placeholder.gif",
        "/wp-content/plugins/revslider/sr6/assets/assets/dummy.png",
        "/img/1x1.gif",
        "/img/blank.gif",
        "/pixel.gif",
        "",
    ],
)
def test_platzhalter_werden_erkannt(url):
    assert ist_platzhalter(url)


@pytest.mark.parametrize("url", ["/uploads/Haus.jpg", "https://cdn.beispiel.de/foto-600x400.webp", "/a/b/bild.png"])
def test_echte_bilder_sind_keine_platzhalter(url):
    assert not ist_platzhalter(url)


def test_srcset_wird_zerlegt():
    erg = srcset_kandidaten("/a.jpg 274w, /b.jpg 600w,  /c.jpg 1200w")
    assert [(u, w) for u, w, _d in erg] == [("/a.jpg", 274), ("/b.jpg", 600), ("/c.jpg", 1200)]


def test_srcset_mit_dichteangaben():
    erg = srcset_kandidaten("/a.jpg 1x, /a2.jpg 2x")
    assert [d for _u, _w, d in erg] == [1.0, 2.0]


def test_srcset_leer_oder_kaputt_kippt_nicht():
    assert srcset_kandidaten("") == []
    assert srcset_kandidaten("   ,  , ") == []


def test_bildquelle_nimmt_das_groesste_element_aus_srcset():
    url, breite = bildquelle(_img('<img src="/k.jpg" srcset="/k.jpg 300w, /g.jpg 1000w, /m.jpg 640w">'))
    assert (url, breite) == ("/g.jpg", 1000)


def test_bildquelle_bevorzugt_data_srcset_vor_platzhalter_src():
    url, breite = bildquelle(
        _img('<img src="data:image/svg+xml,x" data-src="/h.jpg" data-srcset="/h.jpg 600w">')
    )
    assert (url, breite) == ("/h.jpg", 600)


@pytest.mark.parametrize(
    "tag",
    [
        '<img src="data:image/gif;base64,AAAA" data-src="/echt.jpg">',
        '<img src="/lazy_placeholder.gif" data-lazy-src="/echt.jpg">',
        '<img src="/1x1.gif" data-original="/echt.jpg">',
        '<img src="/dummy.png" data-lazyload="/echt.jpg">',
    ],
)
def test_bildquelle_findet_das_echte_bild_hinter_dem_platzhalter(tag):
    assert bildquelle(_img(tag))[0] == "/echt.jpg"


def test_bildquelle_ohne_echtes_bild_ist_leer():
    assert bildquelle(_img('<img src="data:image/gif;base64,AAAA">')) == ("", None)
    assert bildquelle(_img('<img src="/lazy_placeholder.gif">')) == ("", None)


def test_bildquelle_ohne_lazy_load_nimmt_src():
    assert bildquelle(_img('<img src="/foto.jpg">'))[0] == "/foto.jpg"


# --- Inhaltsbild oder Logo? -------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        '<header><img src="/foto.jpg" alt="Titelbild"></header>',
        '<nav><img src="/foto.jpg"></nav>',
        '<footer><img src="/foto.jpg"></footer>',
        '<aside><img src="/foto.jpg"></aside>',
        '<noscript><img src="/foto.jpg"></noscript>',
        '<div role="banner"><img src="/foto.jpg"></div>',
        '<div class="site-header"><img src="/foto.jpg"></div>',
        '<img class="site-logo" src="/foto.jpg">',
        '<img src="/img/logo-klein.png">',
        '<img src="/img/icon-phone.png">',
        '<img src="/foto.jpg" alt="Hotels mit Bewertungen bei HolidayCheck">',
        '<img src="/tracking/pixel.png">',
        '<img src="/foto.jpg" width="32" height="32">',
        '<img src="/foto.jpg" width="1" height="1">',
        '<img src="/vektor.svg">',
    ],
)
def test_logos_icons_pixel_und_kopfbereich_sind_kein_inhalt(html):
    from urllib.parse import urljoin

    knoten = _baum(f"<html><body>{html}</body></html>").css_first("img")
    # Wie in bild_refs(): die Funktion bekommt die aufgeloeste Adresse des Bildes
    assert not ist_inhaltsbild(knoten, urljoin("https://x.de/", knoten.attributes.get("src") or ""))


@pytest.mark.parametrize(
    "html",
    [
        '<main><img src="/foto.jpg" alt="Haus"></main>',
        '<article><img src="/foto.jpg" width="600" height="400"></article>',
        '<div class="content"><img src="/foto.jpg"></div>',
        '<main><img src="/foto.jpg" width="1200"></main>',
    ],
)
def test_fotos_im_inhalt_zaehlen(html):
    wurzel = _baum(f"<html><body>{html}</body></html>")
    assert ist_inhaltsbild(wurzel.css_first("img"), "https://x.de/foto.jpg")


def test_breites_aber_flaches_band_ist_noch_inhalt():
    """800x60 ist ein Banner, kein Icon - nur wenn beide Kanten klein sind, ist es eins."""
    wurzel = _baum('<html><body><main><img src="/b.jpg" width="800" height="60"></main></body></html>')
    assert ist_inhaltsbild(wurzel.css_first("img"), "https://x.de/b.jpg")


# --- ganze Seite ----------------------------------------------------------------


@pytest.fixture
def lazy_refs():
    return bild_refs(_baum(load_fixture("lazyload_seite.html")), "https://beispiel.de/")


def test_lazyload_seite_ohne_platzhalter(lazy_refs):
    urls = [r.url for r in lazy_refs]
    assert not any(u.startswith("data:") for u in urls)
    assert not any("lazy_placeholder" in u or "dummy.png" in u or "1x1" in u or "pixel.gif" in u for u in urls)


def test_lazyload_seite_findet_die_echten_bilder_hinter_den_platzhaltern(lazy_refs):
    urls = {r.url for r in lazy_refs}
    assert "https://beispiel.de/uploads/eifel1.jpg" in urls, "data-lazyload hinter dummy.png"
    assert "https://beispiel.de/uploads/Haus.jpg" in urls, "data-src hinter data:-URI"
    assert "https://beispiel.de/uploads/zimmer-640.jpg" in urls, "groesstes Element aus data-srcset"
    assert "https://beispiel.de/uploads/pool-1000.jpg" in urls, "groesstes Element aus srcset"
    assert "https://beispiel.de/uploads/garten.jpg" in urls, "data-original hinter 1x1-GIF"


def test_lazyload_seite_logos_und_siegel_sind_kein_inhalt(lazy_refs):
    inhalt = {r.url for r in lazy_refs if r.inhalt}
    kein = {r.url for r in lazy_refs if not r.inhalt}
    assert "https://beispiel.de/img/Unbenannt.png" in kein, "Logo im Header"
    assert "https://www.holidaycheck.de/widgets/logo_xlarge.png" in kein, "Bewertungssiegel"
    assert "https://beispiel.de/img/footer-icon.png" in kein, "Icon im Footer"
    assert len(inhalt) == 5


def test_lazyload_seite_zaehlt_keine_kleinen_bilder(lazy_refs):
    """Vor der Korrektur zaehlten Platzhalter und Logos als "zu klein"."""
    gemessen, klein = zaehle_inhaltsbilder(lazy_refs, {})
    assert gemessen == 3, "Haus 600w, Zimmer 640w, Pool 1000w - die anderen sind ohne bekannte Breite"
    assert klein == 0


def test_lazyload_seite_loest_keinen_bilderbefund_aus():
    ctx = make_context("lazyload_seite.html")
    refs = bild_refs(ctx.tree, "https://beispiel.de/")
    gemessen, klein = zaehle_inhaltsbilder(refs, {})
    ctx.assets = AssetInfo(refs=len(refs), images_measured=gemessen, images_small=klein)
    assert "schmaler als 400" not in findings_text(ctx, "bilder")
    assert severity(ctx, "bilder") == 0.0


def test_kleine_inhaltsbilder_werden_weiter_erkannt():
    """Die Korrektur darf echte Mängel nicht verschlucken."""
    refs = bild_refs(_baum(load_fixture("kleine_bilder.html")), "https://auto.de/")
    gemessen, klein = zaehle_inhaltsbilder(refs, {})
    assert (gemessen, klein) == (4, 3)

    ctx = make_context("kleine_bilder.html")
    ctx.assets = AssetInfo(refs=4, images_measured=gemessen, images_small=klein)
    assert "schmaler als 400" in findings_text(ctx, "bilder")


def test_dasselbe_bild_zweimal_zaehlt_einmal():
    html = '<html><body><main><img src="/a.jpg" srcset="/a.jpg 300w"><img src="/a.jpg" srcset="/a.jpg 300w"></main></body></html>'
    assert len(bild_refs(_baum(html), "https://x.de/")) == 1


# --- Zaehlen --------------------------------------------------------------------


def test_breite_kommt_aus_dem_srcset_sonst_aus_dem_dateikopf():
    refs = [BildRef("https://x.de/a.jpg", True, 640), BildRef("https://x.de/b.jpg", True, None)]
    assert zaehle_inhaltsbilder(refs, {"https://x.de/b.jpg": (300, 200)}) == (2, 1)


def test_zaehlpixel_und_logos_zaehlen_nicht():
    refs = [
        BildRef("https://x.de/p.gif", True, None),
        BildRef("https://x.de/l.png", False, 100),
        BildRef("https://x.de/ok.jpg", True, 800),
    ]
    assert zaehle_inhaltsbilder(refs, {"https://x.de/p.gif": (1, 1)}) == (1, 0)


def test_bild_ohne_bekannte_breite_zaehlt_nicht():
    refs = [BildRef("https://x.de/a.jpg", True, None)]
    assert zaehle_inhaltsbilder(refs, {}) == (0, 0)


# --- Schwelle: Argument erst ab drei Inhaltsbildern ---------------------------


@pytest.mark.parametrize("anzahl, erwartet", [(1, False), (2, False), (3, True), (5, True)])
def test_argument_zu_kleine_bilder_erst_ab_drei_inhaltsbildern(anzahl, erwartet):
    ctx = make_context("modern_sauber.html")
    ctx.assets = AssetInfo(refs=anzahl, measured=anzahl, images_measured=anzahl, images_small=anzahl)
    erg = run_check(ctx, "bilder")
    assert ("schmaler als 400" in " ".join(f.text for f in erg.findings)) is erwartet
    assert any(f.argument for f in erg.findings) is erwartet


def test_collect_asset_urls_ueberspringt_platzhalter_und_stellt_inhalt_voran():
    html = (
        '<html><body><header><img src="/logo.png" class="logo"></header>'
        '<main><img src="data:image/gif;base64,AAAA" data-src="/foto.jpg"></main></body></html>'
    )
    bilder, _s, _g = audit.collect_asset_urls(_baum(html), "https://x.de/", limit=10)
    assert bilder == ["https://x.de/foto.jpg", "https://x.de/logo.png"]


# ===========================================================================
# 3. Nicht erreichbar: eigener Status, kein Score
# ===========================================================================


class AttrappenFetcher:
    """Ersetzt den Netzzugriff: liefert eine fertige Seite oder eine Sperre."""

    def __init__(self, seite: Fetched | None = None, robots: bool = True):
        self.seite = seite
        self.robots = robots
        self.aufrufe: list[tuple[str, str]] = []

    async def robots_allows(self, url):
        return self.robots

    async def get_page(self, kandidaten):
        return self.seite

    async def head_status(self, url):
        self.aufrufe.append(("head_status", url))
        return 200

    async def status_und_text(self, url):
        self.aufrufe.append(("status_und_text", url))
        return 200, ""

    async def text_of(self, url):
        self.aufrufe.append(("text_of", url))
        return ""

    async def measure_asset(self, url):
        return None


ARGS = argparse.Namespace(assets=0, pagespeed=False, verzeichnis=False)
LEAD = Lead("Beispiel GmbH", "beispiel.de", "Daun", "Handwerk")


async def _pruefen(seite: Fetched | None = None, robots: bool = True, lead: Lead = LEAD) -> Result:
    return await audit.audit_lead(lead, AttrappenFetcher(seite, robots), ARGS, None)


@pytest.mark.parametrize(
    "fehler, grund",
    [
        ("Timeout beim Laden", "timeout"),
        ("Timeout beim Verbindungsaufbau", "timeout"),
        ("Verbindung fehlgeschlagen ([Errno -2] Name or service not known)", "dns"),
        ("Verbindung fehlgeschlagen ([Errno -5] No address associated with hostname)", "dns"),
        ("Verbindung fehlgeschlagen ([Errno 111] Connection refused)", "verbindung"),
        ("zu viele Weiterleitungen", "weiterleitungen"),
    ],
)
async def test_fehler_beim_abruf_ergibt_status_nicht_erreichbar(fehler, grund):
    res = await _pruefen(Fetched(url="https://beispiel.de", error=fehler))
    assert res.status == STATUS_UNERREICHBAR
    assert res.score is None, "kein Chaos-Score"
    assert res.unerreichbar_grund == grund
    assert not res.bewertet


async def test_server_fehler_5xx_ist_nicht_erreichbar():
    seite = Fetched(url="https://beispiel.de", final_url="https://beispiel.de/", status=503,
                    html="<html><body>Wartung</body></html>")
    res = await _pruefen(seite)
    assert res.status == STATUS_UNERREICHBAR
    assert res.score is None
    assert res.unerreichbar_grund == "http5xx"
    assert "503" in res.befunde[0]


async def test_robots_sperre_ist_nicht_erreichbar():
    res = await _pruefen(robots=False)
    assert (res.status, res.unerreichbar_grund) == (STATUS_UNERREICHBAR, "robots")
    assert res.score is None
    assert res.empfehlung == "manuell ansehen"


async def test_nicht_erreichbar_liefert_keine_verkaufsargumente():
    """Ueber eine Seite, die sich nicht oeffnen laesst, gibt es nichts zu argumentieren."""
    res = await _pruefen(Fetched(url="https://beispiel.de", error="Timeout beim Laden"))
    assert res.argumente == []
    assert res.severities == {}


async def test_nicht_erreichbar_wird_nie_zum_anruf_empfohlen():
    """Auch mit Telefonnummer: die Adresse kann ein Tippfehler in der Quelle sein."""
    lead = Lead("Beispiel GmbH", "beispiel.de", "Daun", "Handwerk", telefon="06591 123456")
    res = await _pruefen(Fetched(url="https://beispiel.de", error="Name or service not known"), lead=lead)
    assert res.empfehlung == "manuell ansehen"
    assert res.telefon == "06591 123456", "die Nummer bleibt zur Hand"


async def test_4xx_wird_weiter_bewertet():
    """Nur 5xx gilt als unerreichbar - bei 404 antwortet ein Server, die Seite wird gewertet."""
    seite = Fetched(url="https://beispiel.de", final_url="https://beispiel.de/", status=404,
                    html="<html><body><p>Seite nicht gefunden</p></body></html>")
    res = await _pruefen(seite)
    assert res.status == STATUS_OK
    assert res.score is not None


async def test_erreichbare_seite_bleibt_bewertet():
    html = load_fixture("modern_sauber.html")
    seite = Fetched(url="https://beispiel.de", final_url="https://beispiel.de/", status=200,
                    html=html, html_bytes=len(html))
    res = await _pruefen(seite)
    assert res.status == STATUS_OK
    assert res.score is not None and res.score < 30
    assert res.unerreichbar_grund == ""


async def test_defekte_seite_wird_weiter_hoch_bewertet():
    """Die Umstellung trifft nur Unerreichbare - eine erreichbare Katastrophe bleibt oben."""
    html = load_fixture("flash_frames.html")
    seite = Fetched(url="http://beispiel.de", final_url="http://beispiel.de/", status=200, html=html,
                    html_bytes=len(html))
    res = await _pruefen(seite)
    assert res.status == STATUS_OK
    assert res.score > 60


# --- Altbestand aus dem Cache ------------------------------------------------


def _alt(status: str, score: float, befunde: list[str], **kw) -> Result:
    return Result(
        name="Alt", url="alt.de", ort="", branche="", final_url="", status=status, score=score,
        cms="", argumente=["alter Satz"], befunde=befunde, severities={}, geprueft_am="2026-10-08T10:00:00",
        empfehlung="Anruf", empfehlung_grund="alt", telefon="06591 1", **kw,
    )


def test_altbestand_fehler_wird_zu_nicht_erreichbar():
    r = _alt("fehler", 100.0, ["[Erreichbarkeit] Seite nicht erreichbar: Verbindung fehlgeschlagen "
                              "([Errno -2] Name or service not known)"])
    assert r.status == STATUS_UNERREICHBAR
    assert r.score is None
    assert r.unerreichbar_grund == "dns"
    assert r.empfehlung == "manuell ansehen", "die alte Empfehlung 'Anruf' faellt weg"


def test_altbestand_gesperrt_wird_zu_robots():
    r = _alt("gesperrt", 0.0, ["Durch robots.txt gesperrt"])
    assert (r.status, r.unerreichbar_grund, r.score) == (STATUS_UNERREICHBAR, "robots", None)


def test_altbestand_interner_fehler_wird_zum_pruefungsfehler():
    r = _alt("fehler", 100.0, ["[Pruefung] Interner Fehler: ValueError: x"])
    assert r.status == STATUS_PRUEFFEHLER
    assert r.score is None


def test_altbestand_ok_mit_5xx_wird_umgestellt():
    r = _alt("ok", 64.0, ["[Erreichbarkeit] Server antwortet mit HTTP 503"])
    assert (r.status, r.unerreichbar_grund, r.score) == (STATUS_UNERREICHBAR, "http5xx", None)


def test_normaler_altbestand_bleibt_unveraendert():
    r = _alt("ok", 42.0, ["[Mobil] Kein Viewport"])
    assert (r.status, r.score) == (STATUS_OK, 42.0)
    assert r.empfehlung == "Anruf", "unbetroffene Eintraege werden nicht angefasst"


def test_cache_liefert_altbestand_umgestellt(tmp_path):
    """Der Weg, den echte Eintraege nehmen: als JSON in die Datenbank, wieder heraus."""
    import json

    cache = audit.Cache(tmp_path / "c.sqlite")
    alt = _alt("fehler", 100.0, ["[Erreichbarkeit] Seite nicht erreichbar: Timeout beim Laden"])
    # Wie von der Vorgaengerversion abgelegt: Status und Score im Rohformat
    cache.conn.execute(
        "INSERT INTO audit (url_key, fetched_at, with_pagespeed, payload) VALUES (?,?,?,?)",
        ("alt.de", "2026-10-08T10:00:00", 0, json.dumps({**alt.__dict__, "status": "fehler", "score": 100.0})),
    )
    cache.conn.commit()
    gelesen = cache.get(Lead("Alt", "alt.de"), 0, False)
    assert gelesen.status == STATUS_UNERREICHBAR and gelesen.score is None
    assert gelesen.unerreichbar_grund == "timeout"
    cache.close()


def test_ergebnis_ohne_score_ueberlebt_den_cache(tmp_path):
    cache = audit.Cache(tmp_path / "c.sqlite")
    res = audit.unbewertet(LEAD, "https://beispiel.de", STATUS_UNERREICHBAR, "dns", "[Erreichbarkeit] x", "2026-10-09T10:00:00")
    cache.put(res, False)
    wieder = cache.get(LEAD, 0, False)
    assert wieder.score is None and wieder.status == STATUS_UNERREICHBAR
    cache.close()


# --- Sortierung und Ausgabe ----------------------------------------------------


def test_nicht_bewertete_sortieren_ans_ende():
    def r(name, score, status=STATUS_OK):
        if score is None:
            return audit.unbewertet(Lead(name, name + ".de"), "", STATUS_UNERREICHBAR, "dns", "x", "t")
        return Result(name, name + ".de", "", "", "", status, score, "", [], [], {}, "t")

    liste = [r("a", None), r("b", 10.0), r("c", 90.0), r("d", None), r("e", 50.0)]
    liste.sort(key=audit.sortierschluessel)
    assert [x.name for x in liste] == ["c", "e", "b", "a", "d"]


def test_csv_laesst_den_score_leer(tmp_path):
    res = audit.unbewertet(LEAD, "", STATUS_UNERREICHBAR, "dns", "[Erreichbarkeit] x", "t")
    ziel = tmp_path / "e.csv"
    audit.write_csv([res], ziel)
    zeile = next(csv.DictReader(ziel.open(encoding="utf-8"), delimiter=";"))
    assert zeile["score"] == ""
    assert zeile["status"] == STATUS_UNERREICHBAR
    assert zeile["empfehlung"] == "manuell ansehen"


def test_html_report_traegt_null_statt_score(tmp_path):
    res = audit.unbewertet(LEAD, "", STATUS_UNERREICHBAR, "dns", "[Erreichbarkeit] x", "t")
    ziel = tmp_path / "e.html"
    audit.write_html([res], ziel, "test")
    html = ziel.read_text(encoding="utf-8")
    assert '"score": null' in html
    assert "r.score == null" in html, "das Skript behandelt fehlende Scores"


def test_konsolentabelle_kommt_mit_fehlendem_score_klar():
    res = audit.unbewertet(LEAD, "", STATUS_UNERREICHBAR, "dns", "[Erreichbarkeit] x", "t")
    ok = Result("Ok", "ok.de", "", "", "", STATUS_OK, 40.0, "", ["Arg"], [], {}, "t")
    audit.print_table([ok, res])  # darf nicht werfen


def test_fortschrittsanzeige_kommt_mit_fehlendem_score_klar():
    assert audit.anzeige_score(None).strip() == "-"
    assert audit.anzeige_score(42.0).strip() == "42.0"


@pytest.mark.parametrize(
    "text, grund",
    [
        ("Timeout beim Laden", "timeout"),
        ("Name or service not known", "dns"),
        ("zu viele Weiterleitungen", "weiterleitungen"),
        ("Server antwortet mit HTTP 502", "http5xx"),
        ("Connection refused", "verbindung"),
    ],
)
def test_grund_aus_fehlermeldung(text, grund):
    assert audit.grund_aus_text(text) == grund


# ===========================================================================
# Platzhalterseiten: "Under construction" und Verwandte
# ===========================================================================


def test_baustellenseite_wird_als_platzhalter_erkannt():
    """Frida am See: Die Domain zeigt nur "Under construction - Awesome site in the
    making!". Das ist keine duenne Website, sondern gar keine."""
    ctx = make_context("baustelle_seite.html")
    assert audit.ist_parkseite(ctx) == "Platzhalterseite"
    assert severity(ctx, "substanz") == 1.0
    assert "keine website" in findings_text(ctx, "substanz")  # findings_text() liefert Kleinbuchstaben


def test_stichwort_nur_im_seitentitel_genuegt():
    """Im sichtbaren Text stand es als "bg_under_construction" (mit Unterstrichen) -
    nur der Titel verriet die Seite."""
    html = ('<html><head><title>Under construction</title></head><body><h1>Willkommen</h1>'
            '<p>Hier tut sich bald etwas.</p></body></html>')
    assert audit.ist_parkseite(make_context(html=html)) == "Platzhalterseite"


@pytest.mark.parametrize(
    "text",
    ["Awesome site in the making", "An amazing site is coming to this web address", "Check back soon!",
     "Webseite im Aufbau", "Unsere Seite ist bald online", "Demnächst online"],
)
def test_weitere_platzhalter_stichworte(text):
    html = f"<html><head><title>Startseite</title></head><body><p>{text}</p></body></html>"
    assert audit.ist_parkseite(make_context(html=html)) == "Platzhalterseite"


def test_lange_echte_seite_mit_stichwort_im_titel_ist_kein_platzhalter():
    """Der Titel allein darf eine echte Firmenseite nicht zur Baustelle erklaeren."""
    absatz = ("Wir sind ein Handwerksbetrieb mit Tradition und beraten Sie persoenlich bei allen Fragen rund um "
              "Ihr Projekt. Rufen Sie uns an oder besuchen Sie uns vor Ort. ") * 12
    html = f"<html><head><title>Meister GmbH - bald online bestellen</title></head><body><p>{absatz}</p></body></html>"
    assert len(absatz) > 1200
    assert audit.ist_parkseite(make_context(html=html)) is None


def test_normale_seite_ist_kein_platzhalter(modern):
    assert audit.ist_parkseite(modern) is None


async def test_baustellenseite_ergibt_100_und_anruf_wenn_eine_nummer_vorliegt():
    """Ende-zu-Ende: keine Website unter der Domain ist der klarste Anlass ueberhaupt."""
    html = load_fixture("baustelle_seite.html")
    seite = Fetched(url="https://frida.example", final_url="https://frida.example/", status=200,
                    html=html, html_bytes=len(html))
    lead = Lead("Frida am See", "frida.example", "Burgwedel", "Gastronomie", telefon="+49 5139 99640")
    res = await _pruefen(seite, lead=lead)
    assert res.status == STATUS_OK
    assert res.score == 100.0
    assert res.severities["substanz"] == 1.0
    assert res.empfehlung == "Anruf"
    assert "keine Website" in res.empfehlung_grund.lower() or "Keine Website" in res.empfehlung_grund
    assert not any("Impressum" in b or "Google Fonts" in b for b in res.befunde), \
        "Befunde ueber die Seite des Anbieters waeren irrefuehrend"


async def test_bei_fehlender_website_kommt_der_hinweis_den_betrieb_zu_pruefen():
    """Eine Platzhalterseite kann Neuanfang oder Aufgabe bedeuten - das weiss das Tool nicht."""
    html = load_fixture("baustelle_seite.html")
    seite = Fetched(url="https://frida.example", final_url="https://frida.example/", status=200,
                    html=html, html_bytes=len(html))
    res = await _pruefen(seite, lead=Lead("Frida", "frida.example", telefon="+49 5139 99640"))
    assert "noch existiert" in res.empfehlung_grund
    assert "aufgegeben" in res.empfehlung_grund
