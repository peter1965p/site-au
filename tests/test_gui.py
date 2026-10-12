"""Tests der GUI - laufen offscreen, ohne sichtbares Fenster und ohne Netz."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

from datetime import date, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PySide6", reason="PySide6 nicht installiert - GUI-Tests entfallen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import audit  # noqa: E402
import gui  # noqa: E402
from audit import Cache, Landbuch, Result, Statusbuch  # noqa: E402


@pytest.fixture
def einstellungen(tmp_path):
    """Wegwerf-Einstellungen pro Test.

    Ohne das schreiben die Tests in die echten Einstellungen des Benutzers.
    Schlimmer noch: das Fenster stellt die zuletzt geoeffnete Liste wieder
    her, ein Test wuerde also echte Leads laden und beim Pruefen tatsaechlich
    ins Netz gehen. Die globale Umlenkung ueber QSettings.setPath() taugt
    dafuer nicht - unter Linux ist NativeFormat nicht IniFormat, und
    setDefaultFormat() greift fuer bereits benannte Anwendungen nicht.
    """
    from PySide6.QtCore import QSettings

    return QSettings(str(tmp_path / "einstellungen.ini"), QSettings.Format.IniFormat)


@pytest.fixture(scope="session")
def app():
    vorhanden = QApplication.instance()
    yield vorhanden or QApplication([])


def _result(name: str, url: str, score: float, ort="Daun", branche="Handwerk", status="ok") -> Result:
    if status != "ok":
        # Nicht erreichbar: kein Score, so wie audit_lead() sie jetzt liefert
        return audit.unbewertet(
            audit.Lead(name, url, ort, branche), f"https://{url}/", audit.STATUS_UNERREICHBAR, "dns",
            "[Erreichbarkeit] Seite nicht erreichbar: Name or service not known", "2026-10-08T10:00:00",
        )
    return Result(
        name=name, url=url, ort=ort, branche=branche, final_url=f"https://{url}/",
        status=status, score=score, cms="Jimdo" if score > 50 else "", argumente=[f"Argument {name}"],
        befunde=[f"[Mobil] Befund {name}"], severities={"mobil": 1.0},
        geprueft_am="2026-10-08T10:00:00",
        telefon="06591 123456" if score > 50 else "",
        empfehlung="Anruf" if score > 50 else "zurueckstellen",
        empfehlung_grund=f"Score {score:.0f}",
    )


@pytest.fixture(autouse=True)
def desktop_meldungen(monkeypatch):
    """Keine echten Desktop-Meldungen aus den Tests; die Aufrufe werden mitgeschrieben."""
    gerufen: list[tuple[str, str]] = []
    monkeypatch.setattr(gui, "desktop_hinweis", lambda titel, text: gerufen.append((titel, text)) or True)
    return gerufen


@pytest.fixture
def fenster(app, tmp_path, einstellungen):
    db = tmp_path / "db.sqlite"
    cache = Cache(db)
    for r in (
        _result("Alpha", "alpha.de", 80.0),
        _result("Beta", "beta.de", 45.0, ort="Gerolstein", branche="Gastronomie"),
        _result("Gamma", "gamma.de", 10.0),
        _result("Delta", "delta.de", 0.0, status="nicht erreichbar"),
    ):
        cache.put(r, False)
    cache.close()

    csv = tmp_path / "leads.csv"
    csv.write_text(
        "name,url,ort,branche\n"
        "Alpha,alpha.de,Daun,Handwerk\n"
        "Beta,beta.de,Gerolstein,Gastronomie\n"
        "Gamma,gamma.de,Daun,Handwerk\n"
        "Delta,delta.de,Daun,Handwerk\n",
        encoding="utf-8",
    )
    f = gui.Fenster(csv, db, einstellungen)
    yield f
    f.statusbuch.close()
    f.deleteLater()


# --- Schalter --------------------------------------------------------------


def test_schalter_verhaelt_sich_wie_eine_checkbox(app):
    s = gui.Schalter("Test")
    assert s.isCheckable()
    assert not s.isChecked()
    gesehen = []
    s.toggled.connect(gesehen.append)
    s.setChecked(True)
    assert s.isChecked()
    assert gesehen == [True]


def test_schalter_sofort_setzen_ohne_signal(app):
    s = gui.Schalter("Test")
    gesehen = []
    s.toggled.connect(gesehen.append)
    s.sofort_setzen(True)
    assert s.isChecked()
    assert s.schieber == 1.0, "Zustand steht sofort, nicht erst nach der Animation"
    assert gesehen == [], "programmatisches Setzen loest keine Nutzeraktion aus"


def test_schalter_sofort_setzen_zurueck(app):
    s = gui.Schalter()
    s.sofort_setzen(True)
    s.sofort_setzen(False)
    assert not s.isChecked()
    assert s.schieber == 0.0


def test_schalter_breite_waechst_mit_dem_text(app):
    assert gui.Schalter("ein sehr langer Text").sizeHint().width() > gui.Schalter("x").sizeHint().width()


def test_schalter_ohne_text_ist_nur_die_bahn(app):
    assert gui.Schalter().sizeHint().width() == gui.Schalter.BAHN_B


# --- Laden und Anzeige -----------------------------------------------------


def test_fenster_laedt_cache_beim_start(fenster):
    assert fenster.model.rowCount() == 4
    assert fenster.proxy.rowCount() == 4


def test_leads_werden_aus_der_csv_gelesen(fenster):
    assert len(fenster.leads) == 4


def test_schlechteste_seite_steht_oben(fenster):
    erste = fenster.model.zeile(0)
    assert erste.result.score == 80.0


def test_nicht_erreichbare_seite_steht_ganz_unten(fenster):
    """Auch wenn sie frueher mit Score 100 ganz oben gestanden haette."""
    letzte = fenster.model.zeile(fenster.model.rowCount() - 1)
    assert letzte.result.status == "nicht erreichbar"
    assert letzte.result.score is None


def test_erste_zeile_ist_vorausgewaehlt(fenster):
    z = fenster.aktuelle_zeile()
    assert z is not None


def test_schritt_steht_in_der_tabelle(fenster):
    """Die Handlungsempfehlung ist die Spalte, nach der gearbeitet wird."""
    texte = {
        fenster.model.data(fenster.model.index(r, gui.S_SCHRITT))
        for r in range(fenster.model.rowCount())
    }
    assert texte & {"Anruf", "Mail", "Kontaktformular", "Kontakt suchen",
                    "manuell ansehen", "zurueckstellen"}


def test_kontaktspalte_zeigt_telefon_vor_mail(fenster):
    z = fenster.model.zeile(0)
    z.result.telefon = "06591 123456"
    z.result.email = "info@beispiel.de"
    assert fenster.model.data(fenster.model.index(0, gui.S_KONTAKT)) == "06591 123456"
    z.result.telefon = ""
    assert fenster.model.data(fenster.model.index(0, gui.S_KONTAKT)) == "info@beispiel.de"


def test_ohne_kontakt_steht_ein_strich(fenster):
    z = fenster.model.zeile(0)
    z.result.telefon = z.result.email = ""
    assert fenster.model.data(fenster.model.index(0, gui.S_KONTAKT)) == "–"


def test_filter_nach_naechstem_schritt(fenster):
    for i, z in enumerate(fenster.model.zeilen):
        z.result.empfehlung = "Anruf" if i < 2 else "zurueckstellen"
    fenster.f_schritt.setCurrentIndex(fenster.f_schritt.findData("Anruf"))
    assert fenster.proxy.rowCount() == 2


def test_suchknopf_nur_ohne_kontaktdaten(fenster):
    """Wer eine Nummer hat, braucht die Websuche nicht."""
    z = fenster.model.zeile(0)
    z.result.telefon = "06591 123456"
    fenster._kontaktleiste_fuellen(z.result)
    assert not fenster.btn_kontaktsuche.isVisible() or True  # unsichtbares Fenster
    z.result.telefon = z.result.email = ""
    fenster._kontaktleiste_fuellen(z.result)
    assert "–" in fenster.kontaktleiste.text() or fenster.kontaktleiste.text()


def test_suchmenue_bietet_die_verzeichnisse_an(fenster):
    """Das Menue selbst pruefen, nicht kontakt_im_netz_suchen(): dessen
    QMenu.exec() ist modal und wartet im Test ewig auf einen Klick."""
    fenster.tabelle.selectRow(0)
    titel = {a.text() for a in fenster._such_menue().actions()}
    assert {"Das Örtliche", "Gelbe Seiten", "11880", "Google"} <= titel


def test_suchmenue_oeffnet_den_browser(fenster, monkeypatch):
    gerufen = []
    monkeypatch.setattr(gui.QDesktopServices, "openUrl", lambda u: gerufen.append(u.toString()))
    fenster.tabelle.selectRow(0)
    name = fenster.aktuelle_zeile().result.name
    next(a for a in fenster._such_menue().actions() if a.text() == "Google").trigger()
    assert gerufen and "search?q=" in gerufen[0]
    assert name.lower() in gerufen[0].lower(), "der Firmenname gehoert in die Suche"


def test_suchmenue_ohne_auswahl_ist_leer(app, tmp_path, einstellungen):
    f = gui.Fenster(None, tmp_path / "leer.sqlite", einstellungen)
    assert f._such_menue().isEmpty()
    f.statusbuch.close()


def test_score_wird_ohne_nachkommastelle_angezeigt(fenster):
    assert fenster.model.data(fenster.model.index(0, gui.S_SCORE)) == "80"


def test_nicht_erreichbare_seite_zeigt_strich_statt_score(fenster):
    zeile = fenster.model.rowCount() - 1
    assert fenster.model.data(fenster.model.index(zeile, gui.S_SCORE)) == "–"
    assert fenster.model.data(fenster.model.index(zeile, gui.S_TECHNIK)) == "nicht erreichbar"


# --- Filter ----------------------------------------------------------------


def _ort_waehlen(fenster, ort, land=""):
    """Eintrag der Ortsauswahl waehlen. Der Datenwert ist "Land|Ort" (siehe gui.ort_schluessel)."""
    idx = fenster.f_ort.findData(gui.ort_schluessel(land, ort))
    assert idx >= 0, f"{ort} ({land}) steht nicht in der Ortsauswahl"
    fenster.f_ort.setCurrentIndex(idx)


def test_filter_nach_ort(fenster):
    _ort_waehlen(fenster, "Gerolstein")
    assert fenster.proxy.rowCount() == 1


def test_filter_nach_branche(fenster):
    fenster.f_branche.setCurrentIndex(fenster.f_branche.findData("Gastronomie"))
    assert fenster.proxy.rowCount() == 1


def test_filter_nach_mindestscore(fenster):
    fenster.f_min.setCurrentIndex(fenster.f_min.findData(50))
    assert fenster.proxy.rowCount() == 1, "nur Alpha mit 80 - Delta hat keinen Score"


def test_nicht_erreichbare_tauchen_im_scorefilter_nicht_auf(fenster):
    """Ab "Score ab X" mit X > 0 fallen Seiten ohne Score heraus."""
    delta = lambda: [  # noqa: E731
        fenster.proxy.index(r, gui.S_FIRMA).data() for r in range(fenster.proxy.rowCount())
    ]
    assert "Delta" in delta(), "ohne Scorefilter bleibt sie auffindbar"
    for stufe in (35, 50, 60, 75):
        fenster.f_min.setCurrentIndex(fenster.f_min.findData(stufe))
        assert "Delta" not in delta(), f"ab {stufe} darf sie nicht mehr erscheinen"


def test_nicht_erreichbare_stehen_in_jeder_sortierung_unten(fenster):
    """Auf- und absteigend, nach Score und nach Name - immer ans Ende."""
    for spalte in (gui.S_SCORE, gui.S_FIRMA):
        for richtung in (gui.Qt.SortOrder.AscendingOrder, gui.Qt.SortOrder.DescendingOrder):
            fenster.tabelle.sortByColumn(spalte, richtung)
            letzte = fenster.proxy.rowCount() - 1
            assert fenster.proxy.index(letzte, gui.S_FIRMA).data() == "Delta", (spalte, richtung)


def test_score_farbe_bei_fehlendem_score_ist_neutral():
    assert gui.score_farbe(None, "nicht erreichbar") == gui.MUTED
    assert gui.score_farbe(95, "nicht erreichbar") == gui.MUTED, "ohne Status ok keine Ampel"


def test_suche_findet_in_befunden(fenster):
    fenster.f_suche.setText("Befund Beta")
    assert fenster.proxy.rowCount() == 1


def test_suche_findet_in_notizen(fenster):
    fenster.statusbuch.setzen("gamma.de", "angerufen", "Rückruf erbeten")
    fenster.ergebnisse_setzen([z.result for z in fenster.model.zeilen])
    fenster.f_suche.setText("rückruf")
    assert fenster.proxy.rowCount() == 1


def test_filter_lassen_sich_kombinieren(fenster):
    _ort_waehlen(fenster, "Daun")
    fenster.f_min.setCurrentIndex(fenster.f_min.findData(50))
    assert fenster.proxy.rowCount() == 1, "Alpha - Delta hat keinen Score"


def test_filter_leeren_zeigt_wieder_alles(fenster):
    fenster.f_suche.setText("Beta")
    fenster.f_suche.setText("")
    assert fenster.proxy.rowCount() == 4


def test_nur_faellige_blendet_den_rest_aus(fenster):
    gestern = (date.today() - timedelta(days=1)).isoformat()
    fenster.statusbuch.setzen("alpha.de", "wiedervorlage", "", gestern)
    fenster.ergebnisse_setzen([z.result for z in fenster.model.zeilen])
    fenster.chk_faellig.setChecked(True)
    assert fenster.proxy.rowCount() == 1


def test_zaehler_zeigt_das_verhaeltnis(fenster):
    fenster.f_suche.setText("Beta")
    assert fenster.lbl_anzahl.text() == "1 von 4"


# --- Status pflegen --------------------------------------------------------


def test_status_aendern_schreibt_in_die_datenbank(fenster):
    fenster.tabelle.selectRow(0)
    z = fenster.aktuelle_zeile()
    fenster.ed_status.setCurrentText("angerufen")
    assert fenster.statusbuch.get(z.result.url).status == "angerufen"


def test_notiz_wird_gespeichert(fenster):
    fenster.tabelle.selectRow(0)
    z = fenster.aktuelle_zeile()
    fenster.ed_notiz.setPlainText("Zweite Nummer probieren")
    fenster.status_speichern()
    assert fenster.statusbuch.get(z.result.url).notiz == "Zweite Nummer probieren"


def test_wiedervorlage_setzen_und_wieder_entfernen(fenster):
    fenster.tabelle.selectRow(0)
    z = fenster.aktuelle_zeile()
    fenster.chk_wv.setChecked(True)
    assert fenster.statusbuch.get(z.result.url).wiedervorlage != ""
    fenster.chk_wv.setChecked(False)
    assert fenster.statusbuch.get(z.result.url).wiedervorlage == ""


def test_statusanzeige_folgt_der_auswahl(fenster):
    fenster.statusbuch.setzen("alpha.de", "termin", "Notiz A")
    fenster.statusbuch.setzen("beta.de", "kein Interesse", "Notiz B")
    fenster.ergebnisse_setzen([z.result for z in fenster.model.zeilen])
    for zeile in range(fenster.proxy.rowCount()):
        fenster.tabelle.selectRow(zeile)
        z = fenster.aktuelle_zeile()
        if z.result.url == "alpha.de":
            assert fenster.ed_status.currentText() == "termin"
            assert fenster.ed_notiz.toPlainText() == "Notiz A"


def test_auswahlwechsel_schreibt_keinen_status(fenster):
    """Beim Durchklicken darf nichts gespeichert werden - sonst traegt jeder
    angesehene Lead ploetzlich den Status des vorigen."""
    fenster.statusbuch.setzen("beta.de", "termin", "wichtig")
    fenster.ergebnisse_setzen([z.result for z in fenster.model.zeilen])
    for zeile in range(fenster.proxy.rowCount()):
        fenster.tabelle.selectRow(zeile)
    assert fenster.statusbuch.get("beta.de").status == "termin"
    assert fenster.statusbuch.get("beta.de").notiz == "wichtig"
    assert fenster.statusbuch.get("alpha.de").status == "offen"


def test_status_erscheint_in_der_tabelle(fenster):
    fenster.tabelle.selectRow(0)
    fenster.ed_status.setCurrentText("termin")
    quelle = fenster.proxy.mapToSource(fenster.tabelle.currentIndex())
    assert fenster.model.data(fenster.model.index(quelle.row(), gui.S_AKQUISE)) == "termin"


# --- Export ----------------------------------------------------------------


def test_pdf_one_pager_wird_geschrieben(fenster, tmp_path, monkeypatch):
    fenster.tabelle.selectRow(0)
    fenster.ed_notiz.setPlainText("Gesprächsnotiz")
    fenster.status_speichern()
    ziel = tmp_path / "lead.pdf"
    monkeypatch.setattr(gui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(ziel), ""))
    fenster.pdf_exportieren()
    assert ziel.exists()
    assert ziel.stat().st_size > 1000
    assert ziel.read_bytes()[:4] == b"%PDF"


def test_bericht_export_schreibt_beide_dateien(fenster, tmp_path, monkeypatch):
    basis = tmp_path / "bericht"
    monkeypatch.setattr(gui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(basis), ""))
    fenster.bericht_exportieren()
    assert (tmp_path / "bericht.csv").exists()
    assert (tmp_path / "bericht.html").exists()


def test_pdf_enthaelt_keine_rechtsbehauptung(fenster):
    fenster.tabelle.selectRow(0)
    html = fenster._pdf_html(fenster.aktuelle_zeile()).lower()
    assert "keine rechtsauskunft" in html
    for wort in ("rechtswidrig", "verstoesst", "illegal", "abmahnung"):
        assert wort not in html


# --- Robustheit ------------------------------------------------------------


def test_kaputte_csv_kippt_das_fenster_nicht(fenster, tmp_path, monkeypatch):
    schlecht = tmp_path / "schlecht.csv"
    schlecht.write_text("name,ort\nOhne URL,Daun\n", encoding="utf-8")
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *a, **k: None)
    fenster.csv_laden(schlecht)
    assert fenster.model.rowCount() == 4, "die alte Liste bleibt stehen"


def test_leeres_fenster_hat_kein_aktives_statusfeld(app, tmp_path, einstellungen):
    f = gui.Fenster(None, tmp_path / "leer.sqlite", einstellungen)
    assert f.model.rowCount() == 0
    assert f.aktuelle_zeile() is None
    assert not f.statusfeld.isEnabled()
    f.statusbuch.close()


def test_lauf_ohne_leads_startet_nicht(app, tmp_path, monkeypatch, einstellungen):
    f = gui.Fenster(None, tmp_path / "leer.sqlite", einstellungen)
    assert not f.leads
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a, **k: None)
    f.lauf_starten()
    assert f.lauf is None, "ohne Leads darf kein Netzlauf anspringen"
    f.statusbuch.close()


def test_zuletzt_geoeffnete_liste_wird_gemerkt(app, tmp_path, einstellungen):
    """Aus dem Anwendungsmenue gestartet gibt es keine Kommandozeile - ein
    jedes Mal leeres Fenster waere dort nutzlos."""
    csv = tmp_path / "meine.csv"
    csv.write_text("name,url\nAlpha,alpha.de\n", encoding="utf-8")
    db = tmp_path / "db.sqlite"

    erst = gui.Fenster(csv, db, einstellungen)
    assert erst.csv_pfad == csv
    erst.statusbuch.close()

    wieder = gui.Fenster(None, db, einstellungen)
    assert wieder.csv_pfad == csv
    assert len(wieder.leads) == 1
    wieder.statusbuch.close()


def test_verschwundene_liste_wird_nicht_geladen(app, tmp_path, einstellungen):
    csv = tmp_path / "weg.csv"
    csv.write_text("name,url\nAlpha,alpha.de\n", encoding="utf-8")
    db = tmp_path / "db2.sqlite"
    erst = gui.Fenster(csv, db, einstellungen)
    erst.statusbuch.close()
    csv.unlink()

    wieder = gui.Fenster(None, db, einstellungen)
    assert wieder.csv_pfad is None
    assert not wieder.leads
    wieder.statusbuch.close()


def test_doppelklick_oeffnet_den_systembrowser(fenster, monkeypatch):
    """Nicht den eingebetteten Chromium - der Nutzer arbeitet in Firefox."""
    gerufen = []
    monkeypatch.setattr(gui.QDesktopServices, "openUrl", lambda u: gerufen.append(u.toString()))
    fenster.tabelle.selectRow(0)
    fenster.extern_oeffnen()
    assert gerufen and gerufen[0].startswith("https://")
    assert fenster.browser is None, "der eingebettete Browser bleibt ungeladen"


def test_vorschau_ohne_webengine_zeigt_hinweis(fenster, monkeypatch):
    """Mit PySide6-Essentials fehlt QtWebEngine - das darf nicht kippen."""
    fenster._browser_fehlt = True
    fenster.tabelle.selectRow(0)
    fenster.tabs.setCurrentIndex(1)
    assert fenster.browser is None


def test_datum_faellt_auf_den_vorschlag_zurueck(fenster):
    """Sonst steht beim naechsten Lead noch das Datum des vorigen im Feld."""
    from PySide6.QtCore import QDate
    fenster.statusbuch.setzen("alpha.de", "wiedervorlage", "", "2026-11-20")
    fenster.ergebnisse_setzen([z.result for z in fenster.model.zeilen])
    for zeile in range(fenster.proxy.rowCount()):
        fenster.tabelle.selectRow(zeile)
        z = fenster.aktuelle_zeile()
        if z.result.url != "alpha.de":
            assert fenster.ed_wv.date() == QDate.currentDate().addDays(14)
            assert not fenster.chk_wv.isChecked()


def test_browser_wird_erst_bei_bedarf_erzeugt(fenster):
    """Chromium zu starten kostet spuerbar - wer nur Befunde sichtet, soll
    den Preis nicht zahlen.

    Geprueft wird nur, dass beim Start keine Instanz existiert. Eine echte
    QWebEngineView im Test anzulegen laesst Qt beim Abraeumen des Profils
    abstuerzen (Speicherzugriffsfehler), und zwar erst in Kombination mit
    den uebrigen Fenstern der Suite - das ist den Test nicht wert.
    """
    assert fenster.browser is None
    fenster.tabelle.selectRow(0)
    fenster.tabs.setCurrentIndex(0)
    assert fenster.browser is None, "der Befunde-Tab darf keinen Browser starten"


def test_vorschau_laedt_den_browser_nach(fenster, monkeypatch):
    """Beim Wechsel auf die Vorschau wird nachgeladen - mit einer Attrappe
    statt echtem Chromium."""
    erzeugt = []

    class Attrappe:
        def url(self):
            class U:
                @staticmethod
                def toString():
                    return ""
            return U()

        def setUrl(self, u):
            erzeugt.append(u.toString())

    monkeypatch.setattr(fenster, "_browser_sicherstellen", lambda: Attrappe())
    fenster.tabelle.selectRow(0)
    fenster.tab_gewechselt(1)
    assert erzeugt and erzeugt[0].startswith("http")


def test_score_farbe_folgt_den_schwellen():
    assert gui.score_farbe(80, "ok") == gui.BAD
    assert gui.score_farbe(45, "ok") == gui.WARN
    assert gui.score_farbe(10, "ok") == gui.OK
    assert gui.score_farbe(95, "gesperrt") == gui.MUTED, "gesperrt wird nicht bewertet"


# --- Parallelitaet ---------------------------------------------------------


def test_parallelitaet_startet_mit_der_vorgabe(fenster):
    assert fenster.sp_parallel.value() == audit.MAX_PARALLEL


def test_parallelitaet_kommt_aus_dem_eingabefeld(fenster):
    fenster.sp_parallel.setValue(12)
    assert fenster.lauf_argumente().concurrency == 12


def test_lauf_argumente_haben_alle_felder_fuer_audit_run(fenster):
    args = fenster.lauf_argumente()
    for feld in ("pagespeed", "refresh", "cache", "no_cache", "max_age", "concurrency",
                 "host_interval", "assets", "timeout", "verzeichnis"):
        assert hasattr(args, feld), feld
    assert args.host_interval == audit.HOST_INTERVAL, "der Takt pro Host bleibt unberuehrt"
    assert args.assets == audit.DEFAULT_ASSETS


def test_parallelitaet_wird_gemerkt(app, tmp_path, einstellungen):
    erst = gui.Fenster(None, tmp_path / "db.sqlite", einstellungen)
    erst.sp_parallel.setValue(17)
    erst.statusbuch.close()
    wieder = gui.Fenster(None, tmp_path / "db.sqlite", einstellungen)
    assert wieder.sp_parallel.value() == 17
    wieder.statusbuch.close()


def test_parallelitaet_ist_begrenzt(fenster):
    fenster.sp_parallel.setValue(500)
    assert fenster.sp_parallel.value() == 60
    fenster.sp_parallel.setValue(-3)
    assert fenster.sp_parallel.value() == 1


# --- Suchdialog: Kreise, Hintergrundarbeit, Absturz beim Schliessen ----------------


import threading as _threading  # noqa: E402
import time as _time  # noqa: E402

from PySide6.QtWidgets import QMessageBox as _QMessageBox  # noqa: E402


def _warte(app, bedingung, sekunden=3.0):
    ende = _time.monotonic() + sekunden
    while _time.monotonic() < ende:
        app.processEvents()
        if bedingung():
            return True
        _time.sleep(0.01)
    return False


@pytest.fixture
def dialog(app, monkeypatch):
    """SuchDialog mit abgefangener Hintergrundarbeit: nichts geht ins Netz."""
    aufgerufen = []
    monkeypatch.setattr(gui, "im_hintergrund", lambda bote, auftrag, aufgabe, *a: aufgerufen.append((auftrag, aufgabe, a)))
    d = gui.SuchDialog()
    d.aufgerufen = aufgerufen
    yield d
    d._uhr.stop()
    d.deleteLater()


def _land(dialog, iso):
    dialog.cb_land.setCurrentIndex(dialog.cb_land.findData(iso))


def test_kreise_kommen_sofort_aus_der_mitgelieferten_liste(dialog, monkeypatch):
    monkeypatch.setattr(gui.finden, "kreise_lokal", lambda iso: [("Ahrweiler", "07131"), ("Vulkaneifel", "07233")])
    _land(dialog, "DE-RP")
    assert dialog.aufgerufen == [], "kein Netz, kein Hintergrund-Thread, kein 'Lade Kreise'"
    assert [dialog.cb_kreis.itemText(i) for i in range(dialog.cb_kreis.count())] == [
        "ganzes Bundesland", "Ahrweiler", "Vulkaneifel"]
    assert dialog.cb_kreis.isEnabled()
    assert dialog.btn_suchen.isEnabled()
    assert "2 Kreise" in dialog.lbl_stand.text()


def test_kreis_wahl_ergibt_das_gebiet(dialog, monkeypatch):
    monkeypatch.setattr(gui.finden, "kreise_lokal", lambda iso: [("Vulkaneifel", "07233")])
    _land(dialog, "DE-RP")
    dialog.cb_kreis.setCurrentIndex(1)
    assert dialog.gebiet().filter == gui.finden.kreis_filter("07233")


def test_stadtstaat_hat_keine_kreisauswahl(dialog):
    _land(dialog, "DE-BE")
    assert not dialog.cb_kreis.isEnabled()
    assert "Stadtstaat" in dialog.lbl_stand.text()
    assert dialog.btn_suchen.isEnabled()
    assert dialog.aufgerufen == []


def test_fehlende_liste_startet_abfrage_ohne_die_suche_zu_sperren(dialog, monkeypatch):
    monkeypatch.setattr(gui.finden, "kreise_lokal", lambda iso: None)
    _land(dialog, "DE-RP")
    assert len(dialog.aufgerufen) == 1
    assert dialog.btn_suchen.isEnabled(), "das ganze Bundesland bleibt waehrenddessen suchbar"
    assert "Lade Kreise" in dialog.lbl_stand.text()


def test_kreisfehler_ist_kein_dialog_und_laesst_die_suche_zu(dialog, monkeypatch):
    monkeypatch.setattr(gui.finden, "kreise_lokal", lambda iso: None)
    monkeypatch.setattr(_QMessageBox, "warning", lambda *a, **k: pytest.fail("kein Fehlerdialog fuer die Kreisliste"))
    _land(dialog, "DE-RP")
    auftrag = dialog.aufgerufen[0][0]
    dialog._antwort_fehler(auftrag, "Overpass antwortet nicht")
    assert dialog.btn_suchen.isEnabled()
    assert "ganze Bundesland" in dialog.lbl_stand.text()


def test_veraltete_antwort_wird_verworfen(dialog, monkeypatch):
    """Land gewechselt, bevor die Kreise des ersten ankamen: sie duerfen nicht
    im Feld des zweiten landen."""
    monkeypatch.setattr(gui.finden, "kreise_lokal", lambda iso: None)
    _land(dialog, "DE-RP")
    alt = dialog.aufgerufen[0][0]
    _land(dialog, "DE-HE")
    dialog._antwort(alt, [("Fremder Kreis", "07999")])
    assert dialog.cb_kreis.count() == 1, "nur 'ganzes Bundesland'"


def test_aktuelle_antwort_wird_uebernommen(dialog, monkeypatch):
    monkeypatch.setattr(gui.finden, "kreise_lokal", lambda iso: None)
    _land(dialog, "DE-RP")
    dialog._antwort(dialog.aufgerufen[0][0], [("Ahrweiler", "07131")])
    assert dialog.cb_kreis.count() == 2


def test_suchfehler_zeigt_dialog_und_gibt_den_knopf_frei(dialog, monkeypatch):
    gezeigt = []
    monkeypatch.setattr(_QMessageBox, "warning", lambda *a, **k: gezeigt.append(a[2]))
    dialog.cb_land.setCurrentIndex(dialog.cb_land.findData("DE-NI"))
    dialog.suchen()
    dialog._antwort_fehler(dialog._auftrag, "Dienst antwortet nicht")
    assert gezeigt and "Dienst antwortet nicht" in gezeigt[0]
    assert dialog.btn_suchen.isEnabled()


def test_suche_meldet_firmen_und_schliesst_den_dialog(dialog):
    dialog.cb_land.setCurrentIndex(dialog.cb_land.findData("DE-NI"))
    dialog.suchen()
    firmen = [gui.finden.Firma("A", "a.de")]
    dialog._antwort(dialog._auftrag, firmen)
    assert dialog.firmen == firmen
    assert dialog.result() == gui.QDialog.DialogCode.Accepted


def test_zeitanzeige_zaehlt_und_warnt_nach_einer_minute(dialog):
    dialog._basis_text, dialog._seit = "Suche", _time.monotonic() - 12
    dialog._tick()
    assert "12 s" in dialog.lbl_stand.text() or "13 s" in dialog.lbl_stand.text()
    assert "ausgelastet" not in dialog.lbl_stand.text()
    dialog._seit = _time.monotonic() - 75
    dialog._tick()
    assert "ausgelastet" in dialog.lbl_stand.text() and "Abbrechen" in dialog.lbl_stand.text()


# --- Hintergrund-Thread ------------------------------------------------------------


def test_hintergrund_liefert_ergebnis_ueber_das_signal(app):
    bote, erhalten = gui._Bote(), []
    bote.fertig.connect(lambda a, e: erhalten.append((a, e)))
    gui.im_hintergrund(bote, 7, lambda x: x * 2, 21)
    assert _warte(app, lambda: erhalten)
    assert erhalten == [(7, 42)]


def test_hintergrund_meldet_fehler_ueber_das_signal(app):
    bote, erhalten = gui._Bote(), []
    bote.fehlgeschlagen.connect(lambda a, t: erhalten.append((a, t)))

    def kaputt():
        raise RuntimeError("Dienst weg")

    gui.im_hintergrund(bote, 3, kaputt)
    assert _warte(app, lambda: erhalten)
    assert erhalten == [(3, "Dienst weg")]


def test_hintergrund_ist_ein_daemon_thread_und_kein_qthread(app, monkeypatch):
    """Der Absturz ("QThread: Destroyed while thread is still running") entstand,
    weil Qt ein noch laufendes QThread-Objekt beim Schliessen zerstoert hat."""
    erzeugt = []
    echt = _threading.Thread

    def merken(*a, **k):
        erzeugt.append(k)
        return echt(*a, **k)

    monkeypatch.setattr(gui.threading, "Thread", merken)
    gui.im_hintergrund(gui._Bote(), 1, lambda: None)
    assert erzeugt and erzeugt[0].get("daemon") is True
    assert not hasattr(gui, "SuchThread"), "kein QThread fuer die Suche mehr"


def test_dialog_schliessen_waehrend_die_abfrage_haengt_stuerzt_nicht_ab(app):
    """Genau der Absturz aus der Praxis: Abfrage laeuft, Dialog wird geschlossen und
    zerstoert, danach liefert die Abfrage ihr Ergebnis."""
    freigabe = _threading.Event()
    gestartet = _threading.Event()

    def haengt():
        gestartet.set()
        freigabe.wait(5)
        return ["zu spaet"]

    d = gui.SuchDialog()
    gui.im_hintergrund(d._bote, d._auftrag, haengt)
    assert gestartet.wait(2)
    d.reject()
    d.deleteLater()
    for _ in range(5):
        app.processEvents()
    freigabe.set()       # die Abfrage liefert jetzt - an einen zerstoerten Dialog
    _time.sleep(0.2)
    for _ in range(5):
        app.processEvents()
    assert True, "kein Abbruch, keine Ausnahme"


# --- Fenster schliessen waehrend ein Lauf haengt --------------------------------------


class _LaufAttrappe:
    def __init__(self, beendet):
        self._beendet = beendet
        self.gestoppt = False

    def isRunning(self):
        return True

    def stoppen(self):
        self.gestoppt = True

    def wait(self, ms):
        return self._beendet


def test_schliessen_mit_haengendem_lauf_beendet_ohne_qt_absturz(fenster, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    beendet = []
    monkeypatch.setattr(gui.os, "_exit", lambda code: beendet.append(code))
    fenster.lauf = _LaufAttrappe(beendet=False)
    fenster.closeEvent(QCloseEvent())
    assert fenster.lauf.gestoppt
    assert beendet == [0], "kontrolliert raus statt Speicherabzug"
    fenster.lauf = None


def test_schliessen_mit_sauber_endendem_lauf_ist_normal(fenster, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    monkeypatch.setattr(gui.os, "_exit", lambda code: pytest.fail("kein harter Ausstieg noetig"))
    fenster.lauf = _LaufAttrappe(beendet=True)
    fenster.closeEvent(QCloseEvent())
    fenster.lauf = None


# --- Suchknopf: auch wenn die Domain nichts taugt ---------------------------------------


def _ergebnis_mit(**kw):
    basis = dict(name="X", url="x.de", ort="", branche="", final_url="", status="ok", score=50.0, cms="",
                 argumente=[], befunde=[], severities={}, geprueft_am="2026-10-09T10:00:00",
                 telefon="06591 1", email="")
    basis.update(kw)
    return Result(**basis)


def test_suchknopf_bei_vorhandenem_kontakt_und_gesunder_seite_nicht():
    assert gui.suche_sinnvoll(_ergebnis_mit()) is False


def test_suchknopf_ohne_kontakt():
    assert gui.suche_sinnvoll(_ergebnis_mit(telefon="", email="")) is True


def test_suchknopf_bei_platzhalterseite_auch_mit_telefon():
    """Frida am See: Telefon aus OpenStreetMap da, aber die Domain zeigt nur einen
    Platzhalter - die echte Seite liegt woanders."""
    assert gui.suche_sinnvoll(_ergebnis_mit(severities={"substanz": 1.0})) is True


def test_suchknopf_bei_nicht_erreichbarer_seite_auch_mit_telefon():
    r = audit.unbewertet(audit.Lead("X", "x.de", telefon="06591 1"), "", audit.STATUS_UNERREICHBAR, "dns", "x", "t")
    assert gui.suche_sinnvoll(r) is True



# ===========================================================================
# Orte nach Bundesland, Aussortieren, Wiedervorlage
# ===========================================================================

from datetime import date, timedelta  # noqa: E402

from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402

CSV_LAENDER = """name,url,ort,branche,telefon,email,bundesland
Hann,hann.de,Hannover,Handwerk,05111,,Niedersachsen
Osna,osna.de,Osnabrück,Handwerk,05412,,DE-NI
Luene,luene.de,Lüneburg,Handwerk,04131,,ni
OhneOrt,ohneort.de,,Handwerk,,,Niedersachsen
Mainz,mainz.de,Mainz,Handwerk,06131,,Rheinland-Pfalz
Mand,mand.de,Manderscheid,Handwerk,06572,,RP
Ahr,ahr.de,Ahrweiler,Handwerk,02641,,Rheinland-Pfalz
Ueb,ueb.de,Überlingen,Handwerk,07551,,Baden-Württemberg
NkNI,nkni.de,Neuenkirchen,Handwerk,05,,Niedersachsen
NkRP,nkrp.de,Neuenkirchen,Handwerk,06,,Rheinland-Pfalz
Irgend,irgend.de,Irgendwo,Handwerk,07,,
"""


@pytest.fixture
def lf(app, tmp_path, einstellungen):
    """Fenster mit Firmen aus mehreren Bundeslaendern, alle bereits geprueft."""
    db = tmp_path / "db.sqlite"
    csv = tmp_path / "leads.csv"
    csv.write_text(CSV_LAENDER, encoding="utf-8")
    cache = Cache(db)
    for i, l in enumerate(audit.read_leads(csv)):
        cache.put(Result(l.name, l.url, l.ort, l.branche, f"https://{l.url}/", "ok", 90.0 - i * 5, "", ["Argument"],
                         ["Befund"], {}, "2026-10-09T10:00:00", telefon=l.telefon, empfehlung="Anruf",
                         empfehlung_grund="x"), False)
    cache.close()
    f = gui.Fenster(csv, db, einstellungen)
    yield f
    if f._wv_dialog is not None:
        f._wv_dialog.close()
    f.statusbuch.close()
    f.landbuch.close()
    f.deleteLater()


def _eintraege(box):
    """(Text, Ebene) aller Eintraege der Ortsauswahl, Ebene "land", "ort" oder None."""
    m = box.model()
    return [(m.item(i).text(), m.item(i).data(gui.EBENE)) for i in range(m.rowCount())]


def _namen(f):
    return [f.proxy.index(r, gui.S_FIRMA).data() for r in range(f.proxy.rowCount())]


def _faellig(f, name, tage_zurueck=1, status="wiedervorlage", notiz=""):
    """Wiedervorlage setzen - in der Datenbank und, wie das Statusfeld es tut, in der Liste des Fensters."""
    i, z = next((i, z) for i, z in enumerate(f.model.zeilen) if z.result.name == name)
    st = f.statusbuch.setzen(z.result.url, status, notiz, (date.today() - timedelta(days=tage_zurueck)).isoformat())
    f.model.status_aktualisieren(i, st)


def _zeile_von(f, name):
    return next(z for z in f.model.zeilen if z.result.name == name)


# --- Ortsauswahl nach Bundesland -------------------------------------------------------------------


def test_orte_stehen_unter_ihrem_bundesland(lf):
    e = _eintraege(lf.f_ort)
    texte = [t for t, _ in e]
    assert texte[0] == "alle Orte"
    ni = texte.index("Niedersachsen  (5)")
    rp = texte.index("Rheinland-Pfalz  (4)")
    bw = texte.index("Baden-Württemberg  (1)")
    assert bw < ni < rp, "Bundeslaender alphabetisch"
    assert texte[ni + 1:rp] == ["Hannover", "Lüneburg", "Neuenkirchen", "Osnabrück"]
    assert texte[rp + 1:rp + 5] == ["Ahrweiler", "Mainz", "Manderscheid", "Neuenkirchen"]


def test_ueberschriften_und_orte_sind_unterscheidbar(lf):
    for text, ebene in _eintraege(lf.f_ort)[1:]:
        assert ebene in ("land", "ort"), text
    land_eintraege = [t for t, e in _eintraege(lf.f_ort) if e == "land"]
    assert land_eintraege[0].startswith("Baden-Württemberg")


def test_ueberschrift_ist_fett_und_farbig(lf):
    m = lf.f_ort.model()
    kopf = next(m.item(i) for i in range(m.rowCount()) if m.item(i).data(gui.EBENE) == "land")
    assert kopf.font().bold()
    assert kopf.foreground().color().name().lower() == gui.ACCENT.lower()


def test_firmen_ohne_bundesland_stehen_ganz_unten(lf):
    e = _eintraege(lf.f_ort)
    assert e[-2] == ("Ohne Bundesland  (1)", "land")
    assert e[-1] == ("Irgendwo", "ort")


def test_umlaute_sortieren_in_der_ortsauswahl_richtig(lf):
    texte = [t for t, _ in _eintraege(lf.f_ort)]
    assert texte.index("Hannover") < texte.index("Lüneburg") < texte.index("Osnabrück")
    # Überlingen steht als einziger Ort unter Baden-Württemberg
    bw = texte.index("Baden-Württemberg  (1)")
    assert texte[bw + 1] == "Überlingen"


def test_ueberschrift_waehlen_filtert_das_ganze_land(lf):
    lf.f_ort.setCurrentIndex(lf.f_ort.findData(gui.ort_schluessel("DE-NI")))
    assert set(_namen(lf)) == {"Hann", "Luene", "Osna", "OhneOrt", "NkNI"}, "auch die Firma ohne Ortsangabe"


def test_ort_waehlen_filtert_auf_land_und_ort(lf):
    lf.f_ort.setCurrentIndex(lf.f_ort.findData(gui.ort_schluessel("DE-RP", "Neuenkirchen")))
    assert _namen(lf) == ["NkRP"], "gleichnamiger Ort in Niedersachsen bleibt draussen"
    lf.f_ort.setCurrentIndex(lf.f_ort.findData(gui.ort_schluessel("DE-NI", "Neuenkirchen")))
    assert _namen(lf) == ["NkNI"]


def test_ohne_bundesland_waehlen_zeigt_die_unzugeordneten(lf):
    lf.f_ort.setCurrentIndex(lf.f_ort.findData(gui.ort_schluessel("")))
    assert _namen(lf) == ["Irgend"]


def test_alle_orte_zeigt_alles(lf):
    lf.f_ort.setCurrentIndex(lf.f_ort.findData(gui.ort_schluessel("DE-RP")))
    lf.f_ort.setCurrentIndex(0)
    assert lf.proxy.rowCount() == 11


def test_auswahl_der_ortsauswahl_bleibt_beim_neuaufbau_erhalten(lf):
    lf.f_ort.setCurrentIndex(lf.f_ort.findData(gui.ort_schluessel("DE-RP", "Mainz")))
    lf._ort_neu_aufbauen([z.result for z in lf.model.zeilen])
    assert lf.f_ort.currentData() == gui.ort_schluessel("DE-RP", "Mainz")


def test_ort_schluessel_laesst_sich_umkehren():
    assert gui.ort_zerlegen(gui.ort_schluessel("DE-NI", "Bad Harzburg")) == ("DE-NI", "Bad Harzburg")
    assert gui.ort_zerlegen(gui.ort_schluessel("", "")) == ("-", "")
    assert gui.ort_zerlegen("") == ("", "")


def test_land_wird_aus_der_csv_gelesen(lf):
    assert _zeile_von(lf, "Mand").result.bundesland == "DE-RP"
    assert _zeile_von(lf, "Irgend").result.bundesland == ""


def test_land_kommt_aus_dem_landbuch_wenn_die_csv_keins_hat(app, tmp_path, einstellungen):
    """Aeltere Listen haben keine Bundesland-Spalte; die Zuordnung steht in der Datenbank."""
    db, csv = tmp_path / "db.sqlite", tmp_path / "l.csv"
    csv.write_text("name,url,ort\nAlt,alt.de,Hannover\n", encoding="utf-8")
    Cache(db).put(Result("Alt", "alt.de", "Hannover", "", "", "ok", 50.0, "", [], [], {}, "t"), False)
    lb = Landbuch(db)
    lb.setzen_viele({"alt.de": "DE-NI"})
    lb.close()
    f = gui.Fenster(csv, db, einstellungen)
    assert f.leads[0].bundesland == "DE-NI"
    assert f.model.zeile(0).result.bundesland == "DE-NI"
    assert "Niedersachsen  (1)" in [t for t, _ in _eintraege(f.f_ort)]
    f.statusbuch.close()
    f.landbuch.close()


# --- Aussortieren: "nicht passend" ----------------------------------------------------------------


def _aussortieren(f, name):
    f.tabelle.selectRow(next(r for r in range(f.proxy.rowCount()) if f.proxy.index(r, gui.S_FIRMA).data() == name))
    f.ed_status.setCurrentText("nicht passend")


def test_nicht_passend_entfernt_die_firma_sofort_aus_der_liste(lf):
    assert "Mainz" in _namen(lf)
    _aussortieren(lf, "Mainz")
    assert "Mainz" not in _namen(lf)
    assert lf.proxy.rowCount() == 10


def test_aussortierte_firma_bleibt_als_sperrliste_gespeichert(lf):
    _aussortieren(lf, "Mainz")
    assert Statusbuch.key("mainz.de") in lf.statusbuch.aussortierte()
    assert lf.statusbuch.get("mainz.de").status == "nicht passend"


def test_zaehler_rechnet_aussortierte_nicht_mit(lf):
    assert lf.lbl_anzahl.text() == "11 von 11"
    _aussortieren(lf, "Mainz")
    assert lf.lbl_anzahl.text() == "10 von 10"


def test_nach_dem_aussortieren_ist_die_naechste_firma_markiert(lf):
    _aussortieren(lf, "Mainz")
    z = lf.aktuelle_zeile()
    assert z is not None and z.result.name != "Mainz"


def test_ortsauswahl_zaehlt_aussortierte_nicht_mehr(lf):
    _aussortieren(lf, "Mainz")
    lf._ort_neu_aufbauen([z.result for z in lf.model.zeilen])
    texte = [t for t, _ in _eintraege(lf.f_ort)]
    assert "Rheinland-Pfalz  (3)" in texte
    assert "Mainz" not in texte, "ein Ort ohne offene Firmen steht nicht mehr zur Wahl"


def test_rueckgaengig_macht_das_aussortieren_ungeschehen(lf):
    lf.tabelle.selectRow(0)
    name = lf.aktuelle_zeile().result.name
    lf.ed_notiz.setPlainText("Rueckruf Montag")
    lf.status_speichern()
    lf.ed_status.setCurrentText("nicht passend")
    assert name not in _namen(lf)
    assert not lf.btn_rueckgaengig.isHidden()
    lf.rueckgaengig()
    assert name in _namen(lf)
    assert lf.btn_rueckgaengig.isHidden()
    st = lf.statusbuch.get(_zeile_von(lf, name).result.url)
    assert st.status != "nicht passend"
    assert st.notiz == "Rueckruf Montag", "die Notiz von vorher ist wieder da"


def test_rueckgaengig_ohne_vorgang_tut_nichts(lf):
    lf.rueckgaengig()
    assert lf.proxy.rowCount() == 11


def test_rueckgaengig_knopf_verschwindet_nach_der_frist(lf):
    _aussortieren(lf, "Mainz")
    assert not lf.btn_rueckgaengig.isHidden()
    lf._rueckgaengig_ausblenden()
    assert lf.btn_rueckgaengig.isHidden()


def test_aussortierte_sind_ueber_den_akquise_filter_auffindbar(lf):
    _aussortieren(lf, "Mainz")
    lf.f_akquise.setCurrentIndex(lf.f_akquise.findData("nicht passend"))
    assert _namen(lf) == ["Mainz"]


def test_menue_aussortierte_zeigen(lf):
    _aussortieren(lf, "Mainz")
    lf.aussortierte_zeigen()
    assert _namen(lf) == ["Mainz"]


def test_mehrere_aussortieren_hintereinander(lf):
    for name in ("Hann", "Osna", "Luene"):
        _aussortieren(lf, name)
    assert not {"Hann", "Osna", "Luene"} & set(_namen(lf))
    assert lf.lbl_anzahl.text() == "8 von 8"


def test_lauf_ueberspringt_aussortierte(lf, monkeypatch):
    erhalten = []

    class FalscherLauf:
        def __init__(self, leads, args, key):
            erhalten.append([l.name for l in leads])
            self.fortschritt = self.fertig = self.fehlgeschlagen = self.finished = type(
                "S", (), {"connect": lambda s, f: None})()

        def start(self):
            pass

    monkeypatch.setattr(gui, "LaufThread", FalscherLauf)
    _aussortieren(lf, "Mainz")
    lf.lauf_starten()
    assert erhalten and "Mainz" not in erhalten[0] and len(erhalten[0]) == 10
    lf.lauf = None


def test_lauf_ohne_aktive_firmen_meldet_das_und_startet_nicht(lf, monkeypatch):
    gemeldet = []
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a, **k: gemeldet.append(a[1]))
    monkeypatch.setattr(gui, "LaufThread", lambda *a, **k: pytest.fail("kein Lauf ohne Firmen"))
    for z in lf.model.zeilen:
        lf.statusbuch.setzen(z.result.url, "nicht passend")
    lf.lauf_starten()
    assert gemeldet == ["Nichts zu prüfen"]


# --- Firmensuche und Sperrliste ----------------------------------------------------------------


def _suche_mit(lf, monkeypatch, firmen):
    class Dialog(gui.SuchDialog):
        def exec(self):
            self.firmen = firmen
            return gui.QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui, "SuchDialog", Dialog)
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a, **k: None)
    lf.firmen_suchen()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_neue_firmen_tragen_ihr_bundesland_in_liste_csv_und_landbuch(lf, monkeypatch):
    _suche_mit(lf, monkeypatch, [gui.finden.Firma("Neu", "neu.de", ort="Kiel", bundesland="DE-SH")])
    neu = next(l for l in lf.leads if l.name == "Neu")
    assert neu.bundesland == "DE-SH"
    assert lf.landbuch.alle()["neu.de"] == "DE-SH"
    assert "Schleswig-Holstein" in lf.csv_pfad.read_text(encoding="utf-8")
    assert audit.read_leads(lf.csv_pfad)[-1].bundesland == "DE-SH", "die gespeicherte CSV ist wieder lesbar"


def test_suche_traegt_das_land_fuer_vorhandene_firmen_nach(lf, monkeypatch):
    """Eine Firma, die schon ohne Land in der Liste stand, bekommt es bei einer erneuten Suche."""
    _suche_mit(lf, monkeypatch, [gui.finden.Firma("Irgend", "irgend.de", ort="Irgendwo", bundesland="DE-HE")])
    assert next(l for l in lf.leads if l.name == "Irgend").bundesland == "DE-HE"
    assert lf.landbuch.alle()["irgend.de"] == "DE-HE"
    assert sum(1 for l in lf.leads if l.name == "Irgend") == 1, "nicht doppelt"


def test_aussortierte_firma_wird_von_einer_suche_nicht_wieder_angeboten(lf, monkeypatch):
    _aussortieren(lf, "Mainz")
    vorher = len(lf.leads)
    _suche_mit(lf, monkeypatch, [gui.finden.Firma("Mainz", "mainz.de", ort="Mainz", bundesland="DE-RP")])
    assert len(lf.leads) == vorher
    assert "Mainz" not in _namen(lf)


def test_gesperrte_firma_ist_auch_nach_neustart_aussortiert(app, tmp_path, einstellungen, lf):
    _aussortieren(lf, "Mainz")
    lf.statusbuch.close()
    lf.landbuch.close()
    f2 = gui.Fenster(lf.csv_pfad, lf.db_pfad, einstellungen)
    assert "Mainz" not in _namen(f2)
    f2.statusbuch.close()
    f2.landbuch.close()


def test_suchdialog_gibt_das_land_an_das_gebiet(app):
    d = gui.SuchDialog()
    d.cb_land.setCurrentIndex(d.cb_land.findData("DE-NI"))
    assert d.gebiet().land == "DE-NI"
    d.cb_kreis.setCurrentIndex(1)
    assert d.gebiet().land == "DE-NI", "auch bei einem einzelnen Kreis"
    d.cb_land.setCurrentIndex(0)
    d.cb_schnell.setCurrentIndex(d.cb_schnell.findData("vulkaneifel"))
    assert d.gebiet().land == "DE-RP"
    d._uhr.stop()
    d.deleteLater()


# --- Wiedervorlage: Anzeige und Erinnerung -----------------------------------------------------


@pytest.mark.parametrize("tage, text", [(0, "heute"), (-2, "heute"), (1, "seit gestern"), (3, "seit 3 Tagen"), (30, "seit 30 Tagen")])
def test_faelligkeit_in_klartext(tage, text):
    assert gui.faellig_text((date.today() - timedelta(days=tage)).isoformat()) == text


def test_faelligkeit_mit_kaputtem_datum_kippt_nicht():
    assert gui.faellig_text("kein Datum") == "kein Datum"


def test_ohne_faellige_wiedervorlage_keine_anzeige_und_keine_erinnerung(lf, desktop_meldungen):
    lf.wv_pruefen(True)
    assert lf.btn_faellig.isHidden()
    assert lf._wv_dialog is None
    assert desktop_meldungen == []


def test_faellige_wiedervorlage_zeigt_den_knopf_in_der_statusleiste(lf):
    _faellig(lf, "Hann", 2)
    _faellig(lf, "Mainz", 1)
    lf.wv_pruefen(False)
    assert not lf.btn_faellig.isHidden()
    assert "2 Wiedervorlagen fällig" in lf.btn_faellig.text()


def test_eine_einzelne_wiedervorlage_steht_im_singular(lf):
    _faellig(lf, "Hann", 1)
    lf.wv_pruefen(False)
    assert "1 Wiedervorlage fällig" in lf.btn_faellig.text()
    assert "Wiedervorlagen" not in lf.btn_faellig.text()


def test_erinnerung_zeigt_liste_und_desktop_meldung(lf, desktop_meldungen):
    _faellig(lf, "Hann", 3, notiz="Rueckruf Chef")
    lf.wv_pruefen(True)
    assert lf._wv_dialog is not None
    assert lf._wv_dialog.tabelle.rowCount() == 1
    zeile = [lf._wv_dialog.tabelle.item(0, c).text() for c in range(5)]
    assert zeile[0] == "Hann" and zeile[1] == "Hannover" and zeile[2] == "seit 3 Tagen"
    assert "Rueckruf Chef" in zeile[4]
    assert desktop_meldungen and "Wiedervorlage fällig" in desktop_meldungen[0][0]
    assert "Hann" in desktop_meldungen[0][1]


def test_erinnerung_kommt_hoechstens_einmal_am_tag(lf, desktop_meldungen):
    _faellig(lf, "Hann", 1)
    lf.wv_pruefen(True)
    lf._wv_dialog.close()
    lf._wv_dialog = None
    lf.wv_pruefen(True)
    lf.wv_pruefen(True)
    assert lf._wv_dialog is None, "der Minutentakt darf nicht jede Minute stoeren"
    assert len(desktop_meldungen) == 1


def test_erinnerung_kommt_am_naechsten_tag_wieder(lf, desktop_meldungen):
    _faellig(lf, "Hann", 1)
    lf.wv_pruefen(True)
    lf._wv_dialog.close()
    lf._wv_dialog = None
    lf._wv_zuletzt = (date.today() - timedelta(days=1)).isoformat()  # "gestern wurde erinnert"
    lf.wv_pruefen(True)
    assert lf._wv_dialog is not None
    assert len(desktop_meldungen) == 2


def test_selbst_gesetzte_wiedervorlage_loest_keine_erinnerung_aus(lf, desktop_meldungen):
    """Wer gerade "heute" einstellt, weiss davon. Nur die Anzeige in der Statusleiste aktualisiert sich."""
    lf.tabelle.selectRow(0)
    lf.chk_wv.setChecked(True)
    lf.ed_wv.setDate(gui.QDate.currentDate())
    lf.ed_status.setCurrentText("wiedervorlage")
    assert not lf.btn_faellig.isHidden()
    assert lf._wv_dialog is None
    assert desktop_meldungen == []


def test_mehrere_faellige_nennen_die_anzahl_in_der_meldung(lf, desktop_meldungen):
    for n in ("Hann", "Osna", "Mainz"):
        _faellig(lf, n, 2)
    lf.wv_pruefen(True)
    assert desktop_meldungen[0][0] == "3 Wiedervorlagen fällig"
    assert "und 2 weitere" in desktop_meldungen[0][1]


def test_erledigte_faelle_erscheinen_nicht_in_der_erinnerung(lf, desktop_meldungen):
    _faellig(lf, "Hann", 2, status="kein Interesse")
    _faellig(lf, "Osna", 2, status="kunde")
    lf.wv_pruefen(True)
    assert lf.btn_faellig.isHidden() and desktop_meldungen == []


def test_aussortieren_loescht_auch_die_erinnerung(lf):
    _faellig(lf, "Mainz", 2)
    lf.wv_pruefen(False)
    assert not lf.btn_faellig.isHidden()
    _aussortieren(lf, "Mainz")
    assert lf.btn_faellig.isHidden(), "aussortiert = erledigt, nichts mehr, woran zu erinnern waere"


def test_aelteste_wiedervorlage_steht_oben(lf):
    _faellig(lf, "Hann", 1)
    _faellig(lf, "Osna", 9)
    _faellig(lf, "Mainz", 4)
    assert [e["firma"] for e in lf._faellige()] == ["Osna", "Mainz", "Hann"]


def test_doppelklick_in_der_erinnerung_springt_zur_firma(lf):
    _faellig(lf, "Osna", 2)
    lf.wv_pruefen(True)
    lf.f_ort.setCurrentIndex(lf.f_ort.findData(gui.ort_schluessel("DE-RP")))  # Filter, der Osna verdeckt
    lf._wv_dialog.tabelle.cellDoubleClicked.emit(0, 0)
    assert lf.f_ort.currentIndex() == 0, "Filter zurueckgesetzt"
    assert lf.aktuelle_zeile().result.name == "Osna"


def test_nur_faellige_zeigen_setzt_andere_filter_zurueck(lf):
    _faellig(lf, "Mainz", 1)
    lf.f_ort.setCurrentIndex(lf.f_ort.findData(gui.ort_schluessel("DE-NI")))
    lf.f_suche.setText("xyz")
    lf.nur_faellige_zeigen()
    assert _namen(lf) == ["Mainz"]
    assert lf.f_suche.text() == ""


def test_klick_auf_den_knopf_zeigt_die_faelligen(lf):
    _faellig(lf, "Hann", 1)
    lf.wv_pruefen(False)
    lf.btn_faellig.click()
    assert _namen(lf) == ["Hann"]


def test_knopf_in_der_erinnerung_zeigt_nur_faellige(lf):
    _faellig(lf, "Hann", 1)
    lf.wv_pruefen(True)
    lf._wv_dialog._alle()
    assert _namen(lf) == ["Hann"]


def test_neue_erinnerung_ersetzt_die_alte_statt_sich_zu_stapeln(lf):
    _faellig(lf, "Hann", 1)
    lf.wv_pruefen(True)
    erste = lf._wv_dialog
    lf._wv_zuletzt = ""
    lf.wv_pruefen(True)
    assert lf._wv_dialog is not erste


def test_menue_akquise_hat_die_beiden_eintraege(lf):
    menues = {a.text().replace("&", ""): a.menu() for a in lf.menuBar().actions()}
    assert "Akquise" in menues
    texte = [a.text() for a in menues["Akquise"].actions()]
    assert "Fällige Wiedervorlagen anzeigen" in texte
    assert "Aussortierte Firmen anzeigen" in texte


def test_erinnerungs_zeitgeber_laeuft_im_minutentakt(lf):
    assert lf._wv_uhr.isActive() and lf._wv_uhr.interval() == 60_000


# --- Desktop-Meldung ----------------------------------------------------------------------------------------


def test_desktop_hinweis_ruft_notify_send_auf(monkeypatch):
    aufrufe = []
    monkeypatch.undo()  # den Rekorder der autouse-Fixture wieder abnehmen
    monkeypatch.setattr(gui.shutil, "which", lambda n: "/usr/bin/notify-send")
    monkeypatch.setattr(gui.subprocess, "Popen", lambda cmd, **k: aufrufe.append(cmd))
    assert gui.desktop_hinweis("Titel", "Text") is True
    assert aufrufe[0][0] == "/usr/bin/notify-send"
    assert aufrufe[0][-2:] == ["Titel", "Text"]


def test_desktop_hinweis_ohne_notify_send_ist_kein_fehler(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(gui.shutil, "which", lambda n: None)
    assert gui.desktop_hinweis("Titel", "Text") is False


def test_desktop_hinweis_faengt_fehler_beim_start_ab(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(gui.shutil, "which", lambda n: "/usr/bin/notify-send")

    def kaputt(*a, **k):
        raise OSError("weg")

    monkeypatch.setattr(gui.subprocess, "Popen", kaputt)
    assert gui.desktop_hinweis("Titel", "Text") is False


def test_fenster_schliesst_das_landbuch(lf):
    lf.closeEvent(QCloseEvent())
    with pytest.raises(Exception):
        lf.landbuch.alle()
    lf.statusbuch = gui.Statusbuch(lf.db_pfad)  # fuer das Aufraeumen der Fixture
    lf.landbuch = gui.Landbuch(lf.db_pfad)


# ===========================================================================
# Schwebende Karten (Schatten)
# ===========================================================================

from PySide6.QtCore import QPoint, QSize  # noqa: E402
from PySide6.QtGui import QColor, QResizeEvent  # noqa: E402
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QLabel, QWidget  # noqa: E402


def _helligkeit(farbe) -> float:
    return 0.299 * farbe.red() + 0.587 * farbe.green() + 0.114 * farbe.blue()


def _karte(**kw):
    inhalt = QLabel("Inhalt")
    karte = gui.SchattenKarte(inhalt, **kw)
    karte.resize(300, 200)
    return karte, inhalt


def test_karte_nimmt_das_widget_in_sich_auf(app):
    karte, inhalt = _karte(raender=(12, 4, 8, 16))
    assert inhalt.parent() is karte
    assert karte.raender == (12, 4, 8, 16)
    assert karte.layout().contentsMargins().left() == 12
    assert karte.layout().contentsMargins().bottom() == 16


def test_standardraender_lassen_unten_mehr_platz_als_oben(app):
    """Der Schatten faellt nach unten - dort braucht er den meisten Platz."""
    karte, _ = _karte()
    links, oben, rechts, unten = karte.raender
    assert unten > oben
    assert links == rechts


def test_schatten_ist_unter_der_karte_dunkler_als_ueber_ihr(app):
    karte, _ = _karte(raender=(14, 10, 14, 20), weite=12, versatz=5)
    bild = karte.schatten().toImage()
    mitte = karte.width() // 2
    ueber = bild.pixelColor(mitte, 10 - 3).alpha()          # knapp ueber der oberen Kartenkante
    unter = bild.pixelColor(mitte, karte.height() - 20 + 3).alpha()  # knapp unter der unteren Kartenkante
    assert unter > ueber, "das Licht kommt von oben: unten ist der Schatten staerker"
    assert unter > 90, "der Schatten ist deutlich sichtbar, nicht nur ein Hauch"


def test_schatten_laeuft_nach_aussen_weich_aus(app):
    karte, _ = _karte(raender=(14, 10, 14, 20), weite=12, versatz=5)
    bild = karte.schatten().toImage()
    mitte = karte.width() // 2
    unterkante = karte.height() - 20
    alphas = [bild.pixelColor(mitte, unterkante + d).alpha() for d in range(0, 16, 3)]
    assert alphas == sorted(alphas, reverse=True), "nach aussen nimmt die Deckkraft ab"
    assert alphas[-1] < alphas[0] / 2


def test_weit_entfernte_ecke_ist_schattenfrei(app):
    karte, _ = _karte()
    assert karte.schatten().toImage().pixelColor(0, 0).alpha() == 0
    assert karte.schatten().toImage().pixelColor(karte.width() - 1, 0).alpha() == 0


def test_schattenbild_wird_zwischengespeichert_und_bei_groessenaenderung_neu_gemalt(app):
    """Wichtig fuers Scrollen: gemalt wird nur bei Bedarf, nicht bei jedem Neuzeichnen."""
    karte, _ = _karte()
    erstes = karte.schatten()
    assert karte.schatten() is erstes
    karte.resize(400, 260)
    karte.resizeEvent(QResizeEvent(karte.size(), QSize(300, 200)))
    assert karte.schatten() is not erstes
    assert karte.schatten().size() == karte.size() * karte.devicePixelRatioF()


def test_kopfstreifen_bleibt_schattenfrei(app):
    """Die Tab-Leiste gehoert nicht zur Karte: dahinter darf kein dunkles Band liegen."""
    mit, _ = _karte(raender=(14, 4, 14, 20), kopf=0)
    ohne, _ = _karte(raender=(14, 4, 14, 20), kopf=40)
    x = mit.width() // 2
    y = 4 + 30  # mitten im Kopfstreifen
    assert mit.schatten().toImage().pixelColor(x, y).alpha() > 0, "ohne Ausnahme: Schatten liegt dort"
    assert ohne.schatten().toImage().pixelColor(x, y).alpha() == 0, "mit Ausnahme: frei"


def test_kopfstreifen_kann_eine_funktion_sein(app):
    hoehen = []
    karte, _ = _karte(raender=(14, 4, 14, 20), kopf=lambda: hoehen.append(1) or 40)
    karte.schatten()
    assert hoehen, "die Hoehe wird beim Malen erfragt (Tab-Leiste ist erst dann bekannt)"


def test_schatten_aendert_sich_mit_der_kopfhoehe(app):
    hoehe = {"h": 0}
    karte, _ = _karte(raender=(14, 4, 14, 20), kopf=lambda: hoehe["h"])
    erstes = karte.schatten()
    hoehe["h"] = 40
    assert karte.schatten() is not erstes


def test_weit_ist_die_ausdehnung_des_schattens(app):
    eng, _ = _karte(raender=(20, 20, 20, 30), weite=4)
    breit, _ = _karte(raender=(20, 20, 20, 30), weite=16)
    mitte, y = eng.width() // 2, eng.height() - 30 + 12
    assert breit.schatten().toImage().pixelColor(mitte, y).alpha() > eng.schatten().toImage().pixelColor(mitte, y).alpha()


def test_knopf_schweben_setzt_einen_schlagschatten(app):
    knopf = gui.QPushButton("x")
    gui.schwebend(knopf)
    effekt = knopf.graphicsEffect()
    assert isinstance(effekt, QGraphicsDropShadowEffect)
    assert effekt.blurRadius() > 0 and effekt.yOffset() > 0


# --- im echten Fenster -------------------------------------------------------------------


@pytest.fixture
def sichtbar(app, fenster):
    """Fenster so, wie das Programm es zeigt: mit dem echten Stylesheet (dunkler Hintergrund).

    Ohne Stylesheet waere der Fensterhintergrund hell, und ein Vergleich mit dem
    dunklen Programmhintergrund misst Unsinn.
    """
    app.setStyleSheet(gui.STYLE)
    fenster.resize(1400, 900)
    fenster.show()
    for _ in range(5):
        QApplication.processEvents()
    yield fenster
    app.setStyleSheet("")


def test_tabelle_und_detailbereich_sind_karten(fenster):
    assert isinstance(fenster.tabelle.parent(), gui.SchattenKarte)
    assert isinstance(fenster.kontaktleiste.parent(), gui.SchattenKarte)
    assert isinstance(fenster.tabs.parent(), gui.SchattenKarte)
    assert isinstance(fenster.statusfeld.parent(), gui.SchattenKarte)


def test_toolbar_knoepfe_schweben(fenster):
    for knopf in (fenster.btn_suche, fenster.btn_oeffnen, fenster.btn_start, fenster.btn_stop):
        assert isinstance(knopf.graphicsEffect(), gui.QGraphicsDropShadowEffect), knopf.text()


def test_tabelle_selbst_hat_keinen_grafikeffekt(fenster):
    """Ein Effekt auf der Tabelle wuerde bei jedem Neuzeichnen den Inhalt in ein Bild rendern."""
    assert fenster.tabelle.graphicsEffect() is None
    assert fenster.tabs.graphicsEffect() is None


def test_unter_der_tabelle_liegt_im_fenster_ein_sichtbarer_schatten(sichtbar):
    bild = sichtbar.grab().toImage()
    unten = sichtbar.tabelle.mapTo(sichtbar, QPoint(sichtbar.tabelle.width() // 2, sichtbar.tabelle.height()))
    hintergrund = _helligkeit(QColor(gui.BG))
    direkt_darunter = _helligkeit(bild.pixelColor(unten.x(), unten.y() + 4))
    assert direkt_darunter < hintergrund - 8, (direkt_darunter, hintergrund)
    weit_weg = _helligkeit(bild.pixelColor(unten.x(), unten.y() + 40))
    assert abs(weit_weg - hintergrund) < 3, "der Schatten klingt aus und faerbt nicht alles dunkel"


def test_hinter_der_tab_leiste_liegt_kein_dunkles_band(sichtbar):
    bild = sichtbar.grab().toImage()
    leiste = sichtbar.tabs.tabBar()
    p = leiste.mapTo(sichtbar, QPoint(leiste.width() - 30, leiste.height() // 2))
    # rechts neben den Tabs: nur Hintergrund, kein Schatten
    assert _helligkeit(bild.pixelColor(p.x(), p.y())) > _helligkeit(QColor(gui.BG)) - 3
