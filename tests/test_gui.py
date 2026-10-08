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
from audit import Cache, Result, Statusbuch  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _einstellungen_isolieren(tmp_path_factory):
    """QSettings auf einen Wegwerfpfad umbiegen.

    Ohne das lesen und schreiben die Tests die echten Einstellungen des
    Benutzers. Schlimmer noch: das Fenster stellt die zuletzt geoeffnete Liste
    wieder her, ein Test wuerde also echte Leads laden und beim Pruefen
    tatsaechlich ins Netz gehen.
    """
    from PySide6.QtCore import QSettings

    pfad = tmp_path_factory.mktemp("qsettings")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(pfad))
    yield pfad


@pytest.fixture(autouse=True)
def _einstellungen_leeren(_einstellungen_isolieren):
    """Jeder Test startet mit leeren Einstellungen.

    Die umgebogenen QSettings gelten fuer die ganze Session; ohne Leeren
    erbt ein Test die zuletzt geoeffnete Liste des vorigen.
    """
    from PySide6.QtCore import QSettings

    QSettings("paeffgen-it", gui.APP_ID).clear()
    yield


@pytest.fixture(scope="session")
def app():
    vorhanden = QApplication.instance()
    yield vorhanden or QApplication([])


def _result(name: str, url: str, score: float, ort="Daun", branche="Handwerk", status="ok") -> Result:
    return Result(
        name=name, url=url, ort=ort, branche=branche, final_url=f"https://{url}/",
        status=status, score=score, cms="Jimdo" if score > 50 else "", argumente=[f"Argument {name}"],
        befunde=[f"[Mobil] Befund {name}"], severities={"mobil": 1.0},
        geprueft_am="2026-10-08T10:00:00",
    )


@pytest.fixture
def fenster(app, tmp_path):
    db = tmp_path / "db.sqlite"
    cache = Cache(db)
    for r in (
        _result("Alpha", "alpha.de", 80.0),
        _result("Beta", "beta.de", 45.0, ort="Gerolstein", branche="Gastronomie"),
        _result("Gamma", "gamma.de", 10.0),
        _result("Delta", "delta.de", 100.0, status="fehler"),
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
    f = gui.Fenster(csv, db)
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
    assert erste.result.score == 100.0


def test_erste_zeile_ist_vorausgewaehlt(fenster):
    z = fenster.aktuelle_zeile()
    assert z is not None


def test_zustand_zeigt_nur_abweichungen(fenster):
    """"geprueft" bei jeder Zeile waere nur Rauschen."""
    texte = {
        fenster.model.data(fenster.model.index(r, gui.S_ZUSTAND)) for r in range(fenster.model.rowCount())
    }
    assert "" in texte
    assert "nicht erreichbar" in texte
    assert "geprüft" not in texte


def test_score_wird_ohne_nachkommastelle_angezeigt(fenster):
    assert fenster.model.data(fenster.model.index(0, gui.S_SCORE)) == "100"


# --- Filter ----------------------------------------------------------------


def test_filter_nach_ort(fenster):
    fenster.f_ort.setCurrentIndex(fenster.f_ort.findData("Gerolstein"))
    assert fenster.proxy.rowCount() == 1


def test_filter_nach_branche(fenster):
    fenster.f_branche.setCurrentIndex(fenster.f_branche.findData("Gastronomie"))
    assert fenster.proxy.rowCount() == 1


def test_filter_nach_mindestscore(fenster):
    fenster.f_min.setCurrentIndex(fenster.f_min.findData(50))
    assert fenster.proxy.rowCount() == 2, "Alpha mit 80 und Delta mit 100"


def test_suche_findet_in_befunden(fenster):
    fenster.f_suche.setText("Befund Beta")
    assert fenster.proxy.rowCount() == 1


def test_suche_findet_in_notizen(fenster):
    fenster.statusbuch.setzen("gamma.de", "angerufen", "Rückruf erbeten")
    fenster.ergebnisse_setzen([z.result for z in fenster.model.zeilen])
    fenster.f_suche.setText("rückruf")
    assert fenster.proxy.rowCount() == 1


def test_filter_lassen_sich_kombinieren(fenster):
    fenster.f_ort.setCurrentIndex(fenster.f_ort.findData("Daun"))
    fenster.f_min.setCurrentIndex(fenster.f_min.findData(50))
    assert fenster.proxy.rowCount() == 2


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


def test_leeres_fenster_hat_kein_aktives_statusfeld(app, tmp_path):
    f = gui.Fenster(None, tmp_path / "leer.sqlite")
    assert f.model.rowCount() == 0
    assert f.aktuelle_zeile() is None
    assert not f.statusfeld.isEnabled()
    f.statusbuch.close()


def test_lauf_ohne_leads_startet_nicht(app, tmp_path, monkeypatch):
    f = gui.Fenster(None, tmp_path / "leer.sqlite")
    assert not f.leads
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a, **k: None)
    f.lauf_starten()
    assert f.lauf is None, "ohne Leads darf kein Netzlauf anspringen"
    f.statusbuch.close()


def test_zuletzt_geoeffnete_liste_wird_gemerkt(app, tmp_path):
    """Aus dem Anwendungsmenue gestartet gibt es keine Kommandozeile - ein
    jedes Mal leeres Fenster waere dort nutzlos."""
    csv = tmp_path / "meine.csv"
    csv.write_text("name,url\nAlpha,alpha.de\n", encoding="utf-8")
    db = tmp_path / "db.sqlite"

    erst = gui.Fenster(csv, db)
    assert erst.csv_pfad == csv
    erst.statusbuch.close()

    wieder = gui.Fenster(None, db)
    assert wieder.csv_pfad == csv
    assert len(wieder.leads) == 1
    wieder.statusbuch.close()


def test_verschwundene_liste_wird_nicht_geladen(app, tmp_path):
    csv = tmp_path / "weg.csv"
    csv.write_text("name,url\nAlpha,alpha.de\n", encoding="utf-8")
    db = tmp_path / "db2.sqlite"
    erst = gui.Fenster(csv, db)
    erst.statusbuch.close()
    csv.unlink()

    wieder = gui.Fenster(None, db)
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
    """Chromium zu starten kostet spuerbar - wer nur Befunde sichtet,
    soll den Preis nicht zahlen."""
    assert fenster.browser is None
    fenster.tabelle.selectRow(0)
    fenster.tabs.setCurrentIndex(1)
    assert fenster.browser is not None
    fenster.browser.setParent(None)
    fenster.browser.deleteLater()


def test_score_farbe_folgt_den_schwellen():
    assert gui.score_farbe(80, "ok") == gui.BAD
    assert gui.score_farbe(45, "ok") == gui.WARN
    assert gui.score_farbe(10, "ok") == gui.OK
    assert gui.score_farbe(95, "gesperrt") == gui.MUTED, "gesperrt wird nicht bewertet"
