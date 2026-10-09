"""Szenario: Firmensuche im Dialog, danach "Pruefen". Kein Test, sondern ein Programm.

Es laeuft als eigener Prozess (siehe test_absturz.py), denn der Fehler, den es
nachstellt, ist ein Speicherzugriffsfehler und riesse sonst die ganze Testsuite mit.

Der Fehler: Der Suchdialog bildet mit seinen Kindelementen Referenzzyklen und bleibt
als Muell liegen. Legt der Pruef-Thread danach zehntausende Objekte an, raeumt
Pythons Speicherbereinigung den Dialog *in diesem Thread* ab - Shiboken loescht die
Qt-Widgets dann ein zweites Mal im Hauptthread und das Programm stuerzt ab.

Aufruf:  python szenario_suche_dann_pruefen.py ARBEITSORDNER N_BASIS N_SUCHE [alt]
"alt" stellt den frueheren Zustand her (Dialog wird nicht abgebaut), um zu zeigen,
dass das Szenario den Fehler tatsaechlich ausloest.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import audit  # noqa: E402
import finden  # noqa: E402
import gui  # noqa: E402

arbeit = Path(sys.argv[1])
n_basis, n_suche = int(sys.argv[2]), int(sys.argv[3])
alt = len(sys.argv) > 4 and sys.argv[4] == "alt"


async def attrappe(lead, fetcher, args, key):
    await asyncio.sleep(0.001)
    return audit.Result(
        name=lead.name, url=lead.url, ort=lead.ort, branche=lead.branche, final_url="https://" + lead.url,
        status="ok", score=10.0, cms="", argumente=[], befunde=[], severities={},
        geprueft_am="2026-10-09T16:44:00",
    )


def falsche_suche(gebiet, branchen, leise=False):
    time.sleep(0.5)
    return [
        finden.Firma(name=f"Suchtreffer {i}", url=f"suchtreffer{i}.example", ort="Daun", branche="Handwerk")
        for i in range(n_suche)
    ]


audit.audit_lead = attrappe
finden.suchen_in = falsche_suche
QMessageBox.information = staticmethod(lambda *a, **k: None)
if alt:
    gui.SuchDialog.deleteLater = lambda self: None  # frueherer Zustand: Dialog bleibt als Muell liegen

csv_pfad = arbeit / "leads.csv"
csv_pfad.write_text(
    "name,url,ort,branche\n" + "".join(f"Basis {i},basis{i}.example,Daun,Handwerk\n" for i in range(n_basis)),
    encoding="utf-8",
)

app = QApplication([])
einstellungen = QSettings(str(arbeit / "einstellungen.ini"), QSettings.Format.IniFormat)
fenster = gui.Fenster(csv_pfad, arbeit / "db.sqlite", einstellungen)
fenster.show()


def treiber() -> None:
    dialog = app.activeModalWidget()
    dialog.cb_land.setCurrentIndex(dialog.cb_land.findData("DE-RP"))
    dialog.suchen()


QTimer.singleShot(300, treiber)
fenster.firmen_suchen()
for _ in range(20):
    app.processEvents()
    time.sleep(0.02)

fenster.lauf_starten()
ende = time.monotonic() + 100
while fenster.lauf is not None and time.monotonic() < ende:
    app.processEvents()
    time.sleep(0.005)
for _ in range(50):
    app.processEvents()
fenster.statusbuch.close()
print(f"LAUF OK: {len(fenster.leads)} Leads, {fenster.model.rowCount()} Zeilen", flush=True)
