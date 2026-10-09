"""Regressionstests zum Absturz "Firmensuche, dann Pruefen" (Speicherzugriffsfehler)."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 nicht installiert")

import shiboken6  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import audit  # noqa: E402
import finden  # noqa: E402
import gui  # noqa: E402

SZENARIO = Path(__file__).parent / "szenario_suche_dann_pruefen.py"


def _szenario(arbeit: Path, modus: str = "neu") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "faulthandler", str(SZENARIO), str(arbeit), "1000", "2000", modus],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )


# --- das Szenario selbst, als eigener Prozess --------------------------------------


def test_firmensuche_und_danach_pruefen_stuerzt_nicht_ab(tmp_path):
    """Die Reihenfolge, die in der Praxis zum Absturz fuehrte: erst im Dialog Firmen
    suchen und uebernehmen, dann "Pruefen" druecken."""
    lauf = _szenario(tmp_path)
    assert lauf.returncode == 0, f"Absturz (Exit {lauf.returncode})\n{lauf.stderr[-1500:]}"
    assert "LAUF OK" in lauf.stdout


def test_das_szenario_loest_den_fehler_ohne_die_korrektur_tatsaechlich_aus(tmp_path):
    """Wachposten fuer den Test darueber: Ein Regressionstest, der auch bei kaputtem
    Code gruen bleibt, ist wertlos. Stuerzt der alte Zustand hier nicht mehr ab (etwa
    weil PySide den Fehler behebt), taugt das Szenario nicht mehr als Nachweis - dann
    diesen Test und das Szenario ueberpruefen."""
    lauf = _szenario(tmp_path, "alt")
    assert lauf.returncode != 0, "das Szenario stellt den Absturz nicht mehr nach"


# --- die Bausteine der Korrektur ---------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_dialog_wird_nach_der_firmensuche_im_hauptthread_abgebaut(app, tmp_path, monkeypatch):
    """Der Dialog bildet Referenzzyklen. Ohne sofortigen Abbau holt ihn spaeter die
    Speicherbereinigung - womoeglich im Pruef-Thread."""
    from PySide6.QtCore import QSettings

    instanzen: list = []

    class Dialog(gui.SuchDialog):
        def exec(self):
            instanzen.append(self)
            self.firmen = [finden.Firma("Testfirma", "testfirma.example", ort="Daun")]
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui, "SuchDialog", Dialog)
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a, **k: None)
    csv = tmp_path / "leads.csv"
    csv.write_text("name,url\nAlt,alt.example\n", encoding="utf-8")
    einstellungen = QSettings(str(tmp_path / "e.ini"), QSettings.Format.IniFormat)
    fenster = gui.Fenster(csv, tmp_path / "db.sqlite", einstellungen)

    fenster.firmen_suchen()
    assert len(instanzen) == 1
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    assert not shiboken6.isValid(instanzen[0]), "das Qt-Objekt des Dialogs ist noch am Leben"
    assert [l.url for l in fenster.leads] == ["alt.example", "testfirma.example"], "die Treffer sind uebernommen"
    fenster.statusbuch.close()


def test_abgebrochene_suche_baut_den_dialog_ebenfalls_ab(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    instanzen: list = []

    class Dialog(gui.SuchDialog):
        def exec(self):
            instanzen.append(self)
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(gui, "SuchDialog", Dialog)
    csv = tmp_path / "leads.csv"
    csv.write_text("name,url\nAlt,alt.example\n", encoding="utf-8")
    fenster = gui.Fenster(csv, tmp_path / "db.sqlite", QSettings(str(tmp_path / "e.ini"), QSettings.Format.IniFormat))
    fenster.firmen_suchen()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    assert not shiboken6.isValid(instanzen[0])
    assert len(fenster.leads) == 1, "nichts uebernommen"
    fenster.statusbuch.close()


@pytest.fixture
def gc_zurueck():
    """Die Speicherbereinigung nach dem Test wieder einschalten."""
    war_an = gc.isenabled()
    yield
    gc.enable() if war_an else gc.disable()


def test_speicherbereinigung_laeuft_nur_noch_aus_dem_hauptthread(app, gc_zurueck):
    uhr = gui.gc_im_hauptthread(app)
    try:
        assert not gc.isenabled(), "keine automatische Bereinigung im Thread, der gerade Objekte anlegt"
        assert uhr.isActive()
        assert uhr.interval() == 2000
    finally:
        uhr.stop()


def test_zeitgeber_raeumt_oft_die_junge_und_selten_die_ganze_generation_auf(app, gc_zurueck, monkeypatch):
    stufen: list[int] = []
    monkeypatch.setattr(gui.gc, "collect", lambda generation=2: stufen.append(generation) or 0)
    uhr = gui.gc_im_hauptthread(app)
    try:
        for _ in range(60):
            uhr.timeout.emit()
        assert stufen.count(2) == 2, "alle 30 Takte vollstaendig"
        assert stufen.count(1) == 58
    finally:
        uhr.stop()


def test_gui_schaltet_die_absturzspur_ein():
    """Ohne faulthandler bleibt von einem Speicherzugriffsfehler nur 'Speicherabzug
    geschrieben'; mit ihm steht im Terminal, wo es passiert ist."""
    quelle = (Path(gui.__file__)).read_text(encoding="utf-8")
    assert "faulthandler.enable()" in quelle
    assert "gc_im_hauptthread(app)" in quelle
