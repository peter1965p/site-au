#!/usr/bin/env python3
"""site-audit GUI - Leads pruefen, sichten und den Akquise-Status pflegen.

Aufruf:  python gui.py [leads.csv]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QEasingCurve,
    QModelIndex,
    QPropertyAnimation,
    QRectF,
    QSize,
    QSortFilterProxyModel,
    Property,
    Qt,
    QThread,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QKeySequence,
    QPainter,
    QPalette,
    QTextDocument,
)
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStyledItemDelegate,
    QTableView,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

import audit
from audit import Cache, Lead, LeadStatus, Result, Statusbuch

# Farben passend zum HTML-Report
BG = "#11151c"
PANEL = "#171d26"
PANEL2 = "#1e2633"
LINE = "#2a3443"
FG = "#e4e9f0"
MUTED = "#93a0b4"
ACCENT = "#5aa9f7"
BAD = "#ef4a5c"
WARN = "#e8a33d"
OK = "#3fb97a"

SCHWELLE_ROT = 60.0
SCHWELLE_GELB = 35.0


def score_farbe(score: float, status: str) -> str:
    if status == "gesperrt":
        return MUTED
    if score >= SCHWELLE_ROT:
        return BAD
    if score >= SCHWELLE_GELB:
        return WARN
    return OK


# ---------------------------------------------------------------------------
# Schiebeschalter
# ---------------------------------------------------------------------------


class Schalter(QAbstractButton):
    """Ein Schiebeschalter anstelle einer Checkbox.

    Qt bringt keinen mit. Weil das Widget von QAbstractButton erbt, laesst es
    sich genau wie eine QCheckBox ansprechen - isChecked(), setChecked() und
    das Signal toggled() verhalten sich identisch.
    """

    BAHN_B = 40
    BAHN_H = 22
    RAND = 3

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._schieber = 0.0
        self._anim = QPropertyAnimation(self, b"schieber", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.toggled.connect(self._animieren)

    def _get_schieber(self) -> float:
        return self._schieber

    def _set_schieber(self, wert: float) -> None:
        self._schieber = wert
        self.update()

    schieber = Property(float, _get_schieber, _set_schieber)

    def _animieren(self, an: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._schieber)
        self._anim.setEndValue(1.0 if an else 0.0)
        self._anim.start()

    def sofort_setzen(self, an: bool) -> None:
        """Zustand ohne Animation uebernehmen.

        Beim Wechsel zwischen Leads soll der Schalter sofort stimmen; eine
        Animation sieht dort nach einer Eingabe des Nutzers aus, die es nicht
        gab, und waere bei schnellem Durchklicken nur Flackern.
        """
        self._anim.stop()
        blockiert = self.blockSignals(True)
        self.setChecked(an)
        self.blockSignals(blockiert)
        self._schieber = 1.0 if an else 0.0
        self.update()

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        breite = self.BAHN_B + (8 + fm.horizontalAdvance(self.text()) if self.text() else 0)
        return QSize(breite, max(self.BAHN_H, fm.height()))

    @staticmethod
    def _mischen(a: QColor, b: QColor, t: float) -> QColor:
        return QColor(
            round(a.red() + (b.red() - a.red()) * t),
            round(a.green() + (b.green() - a.green()) * t),
            round(a.blue() + (b.blue() - a.blue()) * t),
        )

    def paintEvent(self, _event) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        aktiv = self.isEnabled()
        t = self._schieber

        aus = QColor("#151b24" if not aktiv else PANEL2)
        an = QColor("#2c4a66" if not aktiv else ACCENT)
        bahn_farbe = self._mischen(aus, an, t)
        rahmen = self._mischen(QColor("#262f3c" if not aktiv else "#3a4658"), bahn_farbe, t)

        oben = (self.height() - self.BAHN_H) / 2
        bahn = QRectF(0, oben, self.BAHN_B, self.BAHN_H)
        p.setBrush(bahn_farbe)
        p.setPen(rahmen)
        p.drawRoundedRect(bahn.adjusted(0.5, 0.5, -0.5, -0.5), self.BAHN_H / 2, self.BAHN_H / 2)

        d = self.BAHN_H - 2 * self.RAND
        x = self.RAND + t * (self.BAHN_B - 2 * self.RAND - d)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#6b7688" if not aktiv else "#f2f6fb" if t > 0.5 else "#8a97aa"))
        p.drawEllipse(QRectF(x, oben + self.RAND, d, d))

        if self.text():
            p.setPen(QColor(FG if aktiv else "#5a6676"))
            p.drawText(
                QRectF(self.BAHN_B + 8, 0, self.width() - self.BAHN_B - 8, self.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self.text(),
            )

        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QColor(ACCENT))
            p.drawRoundedRect(bahn.adjusted(-2, -2, 2, 2), self.BAHN_H / 2 + 2, self.BAHN_H / 2 + 2)


# ---------------------------------------------------------------------------
# Hintergrundlauf
# ---------------------------------------------------------------------------


class LaufThread(QThread):
    """Faehrt den asyncio-Lauf in einem eigenen Thread.

    SQLite-Verbindungen gehoeren dem Thread, der sie oeffnet - der Cache wird
    deshalb hier drin erzeugt und nicht aus dem GUI-Thread hereingereicht.
    """

    fortschritt = Signal(int, int, object, bool)
    fertig = Signal(object)
    fehlgeschlagen = Signal(str)

    def __init__(self, leads: list[Lead], args: argparse.Namespace, psi_key: str | None) -> None:
        super().__init__()
        self.leads = leads
        self.args = args
        self.psi_key = psi_key
        self._stop = False

    def stoppen(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102 - QThread-Einstieg
        cache = None
        try:
            if not self.args.no_cache:
                cache = Cache(self.args.cache)
            ergebnisse = asyncio.run(
                audit.run(
                    self.leads,
                    self.args,
                    cache,
                    self.psi_key,
                    melde=lambda f, g, r, c: self.fortschritt.emit(f, g, r, c),
                    abbruch=lambda: self._stop,
                )
            )
            self.fertig.emit(ergebnisse)
        except Exception as exc:  # noqa: BLE001 - Fehler gehoeren in die GUI, nicht in die Konsole
            self.fehlgeschlagen.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if cache is not None:
                cache.close()


# ---------------------------------------------------------------------------
# Tabellenmodell
# ---------------------------------------------------------------------------


@dataclass
class Zeile:
    result: Result
    status: LeadStatus


# Branche steht bewusst nicht in der Tabelle: sie dient als Filter und steht
# im Detailbereich. Der Firmenname ist die wichtigste Spalte und braucht Platz.
SPALTEN = ["Score", "Firma", "Ort", "Technik", "Zustand", "Akquise", "Wiedervorlage"]
S_SCORE, S_FIRMA, S_ORT, S_TECHNIK, S_ZUSTAND, S_AKQUISE, S_WV = range(7)

ROLLE_SORT = Qt.ItemDataRole.UserRole + 1
ROLLE_ZEILE = Qt.ItemDataRole.UserRole + 2


class LeadModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.zeilen: list[Zeile] = []

    def setzen(self, zeilen: list[Zeile]) -> None:
        self.beginResetModel()
        self.zeilen = zeilen
        self.endResetModel()

    def zeile(self, row: int) -> Zeile | None:
        return self.zeilen[row] if 0 <= row < len(self.zeilen) else None

    def status_aktualisieren(self, row: int, status: LeadStatus) -> None:
        if 0 <= row < len(self.zeilen):
            self.zeilen[row].status = status
            self.dataChanged.emit(self.index(row, S_AKQUISE), self.index(row, S_WV))

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.zeilen)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(SPALTEN)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return SPALTEN[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        z = self.zeilen[index.row()]
        r, st = z.result, z.status
        col = index.column()

        if role == ROLLE_ZEILE:
            return z

        if role == ROLLE_SORT:
            return {
                S_SCORE: r.score,
                S_FIRMA: r.name.lower(),
                S_ORT: r.ort.lower(),
                S_TECHNIK: (r.cms or "").lower(),
                S_ZUSTAND: r.status,
                S_AKQUISE: st.status,
                S_WV: st.wiedervorlage or "9999",
            }[col]

        if role == Qt.ItemDataRole.DisplayRole:
            if col == S_SCORE:
                return f"{r.score:.0f}"
            if col == S_FIRMA:
                return r.name
            if col == S_ORT:
                return r.ort or "–"
            if col == S_TECHNIK:
                return r.cms or "–"
            if col == S_ZUSTAND:
                # "geprueft" ist der Normalfall und steht bei jeder Zeile - das
                # waere nur Rauschen. Nur Abweichungen bekommen Text.
                return {"fehler": "nicht erreichbar", "gesperrt": "gesperrt"}.get(r.status, "")
            if col == S_AKQUISE:
                return st.status
            if col == S_WV:
                if not st.wiedervorlage:
                    return ""
                faellig = st.wiedervorlage <= date.today().isoformat()
                return ("⚑ " if faellig else "") + st.wiedervorlage

        if role == Qt.ItemDataRole.ToolTipRole:
            teile = [f"{r.name} — Score {r.score:.0f}", r.final_url or r.url]
            if r.argumente:
                teile += ["", *(f"• {a}" for a in r.argumente)]
            if st.notiz:
                teile += ["", f"Notiz: {st.notiz}"]
            return "\n".join(teile)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == S_ZUSTAND and r.status == "fehler":
                return QColor(BAD)
            if col == S_WV and st.wiedervorlage and st.wiedervorlage <= date.today().isoformat():
                return QColor(WARN)
            if col == S_AKQUISE and st.status != "offen":
                return QColor(ACCENT)
            if col in (S_ORT, S_TECHNIK):
                return QColor(MUTED)

        if role == Qt.ItemDataRole.FontRole and col == S_FIRMA:
            f = QFont()
            f.setBold(True)
            return f

        if role == Qt.ItemDataRole.TextAlignmentRole and col == S_SCORE:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        return None


class ScoreDelegate(QStyledItemDelegate):
    """Zeichnet den Score als Zahl mit Balken darunter."""

    def paint(self, painter: QPainter, option, index) -> None:
        super().paint(painter, option, index)
        z = index.data(ROLLE_ZEILE)
        if z is None:
            return
        farbe = QColor(score_farbe(z.result.score, z.result.status))
        rect = option.rect
        breite = max(2, int((rect.width() - 16) * min(100.0, z.result.score) / 100))
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(LINE))
        painter.drawRoundedRect(rect.left() + 8, rect.bottom() - 7, rect.width() - 16, 3, 1.5, 1.5)
        painter.setBrush(farbe)
        painter.drawRoundedRect(rect.left() + 8, rect.bottom() - 7, breite, 3, 1.5, 1.5)
        painter.restore()

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        z = index.data(ROLLE_ZEILE)
        if z is not None:
            option.palette.setColor(
                QPalette.ColorRole.Text, QColor(score_farbe(z.result.score, z.result.status))
            )
            f = option.font
            f.setBold(True)
            # Bei pixelbasierten Schriften liefert pointSizeF() -1; dann waere
            # -1 + 1 == 0 und Qt verwirft die Groesse mit einer Warnung.
            if f.pointSizeF() > 0:
                f.setPointSizeF(f.pointSizeF() + 1)
            elif f.pixelSize() > 0:
                f.setPixelSize(f.pixelSize() + 1)
            option.font = f


class LeadFilter(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.ort = ""
        self.branche = ""
        self.akquise = ""
        self.suche = ""
        self.min_score = 0.0
        self.nur_faellig = False
        self.setSortRole(ROLLE_SORT)

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        model: LeadModel = self.sourceModel()
        z = model.zeile(row)
        if z is None:
            return False
        r, st = z.result, z.status
        if self.ort and r.ort != self.ort:
            return False
        if self.branche and r.branche != self.branche:
            return False
        if self.akquise and st.status != self.akquise:
            return False
        if r.score < self.min_score:
            return False
        if self.nur_faellig:
            if not st.wiedervorlage or st.wiedervorlage > date.today().isoformat():
                return False
        if self.suche:
            heu = " ".join(
                [r.name, r.url, r.ort, r.branche, r.cms or "", st.notiz, *r.argumente, *r.befunde]
            ).lower()
            if self.suche not in heu:
                return False
        return True


# ---------------------------------------------------------------------------
# Hauptfenster
# ---------------------------------------------------------------------------


class Fenster(QMainWindow):
    def __init__(self, csv_pfad: Path | None, db_pfad: Path) -> None:
        super().__init__()
        self.setWindowTitle("site-audit")
        self.resize(1500, 900)

        self.db_pfad = db_pfad
        self.statusbuch = Statusbuch(db_pfad)
        self.csv_pfad: Path | None = None
        self.leads: list[Lead] = []
        self.lauf: LaufThread | None = None
        self._lade_sperre = False

        audit.load_env_file(Path(".env"))
        self.psi_key = (os.environ.get("PSI_KEY") or "").strip() or None

        self.model = LeadModel()
        self.proxy = LeadFilter()
        self.proxy.setSourceModel(self.model)

        self._baue_oberflaeche()
        self._baue_menue()

        if csv_pfad:
            self.csv_laden(csv_pfad)
        else:
            self.aus_cache_laden()

    # --- Aufbau ------------------------------------------------------------

    def _baue_oberflaeche(self) -> None:
        zentral = QWidget()
        aussen = QVBoxLayout(zentral)
        aussen.setContentsMargins(10, 10, 10, 6)
        aussen.setSpacing(8)

        aussen.addWidget(self._baue_leiste())

        teiler = QSplitter(Qt.Orientation.Horizontal)
        teiler.addWidget(self._baue_tabelle())
        teiler.addWidget(self._baue_detail())
        teiler.setStretchFactor(0, 4)
        teiler.setStretchFactor(1, 5)
        teiler.setSizes([800, 700])
        aussen.addWidget(teiler, 1)

        self.setCentralWidget(zentral)

        self.fortschritt = QProgressBar()
        self.fortschritt.setMaximumWidth(260)
        self.fortschritt.setVisible(False)
        self.statusBar().addPermanentWidget(self.fortschritt)
        self.statusBar().showMessage("Bereit")

    def _baue_leiste(self) -> QWidget:
        leiste = QWidget()
        lay = QHBoxLayout(leiste)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.btn_oeffnen = QPushButton("CSV öffnen …")
        self.btn_oeffnen.clicked.connect(self.csv_dialog)
        lay.addWidget(self.btn_oeffnen)

        self.btn_start = QPushButton("▶  Prüfen")
        self.btn_start.setObjectName("start")
        self.btn_start.clicked.connect(self.lauf_starten)
        lay.addWidget(self.btn_start)

        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.lauf_stoppen)
        lay.addWidget(self.btn_stop)

        self.chk_refresh = Schalter("neu prüfen")
        self.chk_refresh.setToolTip("Cache ignorieren und alle Seiten erneut abrufen")
        lay.addWidget(self.chk_refresh)

        self.chk_psi = Schalter("PageSpeed")
        self.chk_psi.setEnabled(self.psi_key is not None)
        self.chk_psi.setToolTip(
            "Google PageSpeed Insights mitprüfen (ca. 15 s pro Seite)"
            if self.psi_key
            else "Kein PSI_KEY in .env gefunden"
        )
        lay.addWidget(self.chk_psi)

        lay.addSpacing(16)

        self.f_ort = QComboBox()
        self.f_ort.addItem("alle Orte", "")
        self.f_ort.currentIndexChanged.connect(self.filter_anwenden)
        lay.addWidget(self.f_ort)

        self.f_branche = QComboBox()
        self.f_branche.addItem("alle Branchen", "")
        self.f_branche.currentIndexChanged.connect(self.filter_anwenden)
        lay.addWidget(self.f_branche)

        self.f_akquise = QComboBox()
        self.f_akquise.addItem("jeder Akquise-Status", "")
        for w in audit.STATUS_WERTE:
            self.f_akquise.addItem(w, w)
        self.f_akquise.currentIndexChanged.connect(self.filter_anwenden)
        lay.addWidget(self.f_akquise)

        self.f_min = QComboBox()
        for label, wert in (("Score ab 0", 0), ("ab 35", 35), ("ab 50", 50), ("ab 60", 60), ("ab 75", 75)):
            self.f_min.addItem(label, wert)
        self.f_min.currentIndexChanged.connect(self.filter_anwenden)
        lay.addWidget(self.f_min)

        self.chk_faellig = Schalter("nur fällige")
        self.chk_faellig.setToolTip("Nur Leads mit fälliger Wiedervorlage")
        self.chk_faellig.toggled.connect(self.filter_anwenden)
        lay.addWidget(self.chk_faellig)

        self.f_suche = QLineEdit()
        self.f_suche.setPlaceholderText("Suche in Name, Technik, Befunden, Notizen …")
        self.f_suche.setClearButtonEnabled(True)
        self.f_suche.textChanged.connect(self.filter_anwenden)
        lay.addWidget(self.f_suche, 1)

        self.lbl_anzahl = QLabel("")
        self.lbl_anzahl.setStyleSheet(f"color: {MUTED};")
        lay.addWidget(self.lbl_anzahl)

        return leiste

    def _baue_tabelle(self) -> QWidget:
        self.tabelle = QTableView()
        self.tabelle.setModel(self.proxy)
        self.tabelle.setSortingEnabled(True)
        self.tabelle.sortByColumn(S_SCORE, Qt.SortOrder.DescendingOrder)
        self.tabelle.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.tabelle.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.tabelle.setAlternatingRowColors(True)
        self.tabelle.verticalHeader().setVisible(False)
        self.tabelle.verticalHeader().setDefaultSectionSize(34)
        self.tabelle.setItemDelegateForColumn(S_SCORE, ScoreDelegate(self.tabelle))
        self.tabelle.setWordWrap(False)
        self.tabelle.setMinimumWidth(680)
        self.tabelle.setHorizontalScrollMode(QTableView.ScrollMode.ScrollPerPixel)

        kopf = self.tabelle.horizontalHeader()
        kopf.setMinimumSectionSize(64)
        kopf.setSectionResizeMode(S_FIRMA, QHeaderView.ResizeMode.Stretch)
        for c, breite in ((S_SCORE, 74), (S_ORT, 116), (S_TECHNIK, 138),
                          (S_ZUSTAND, 112), (S_AKQUISE, 118), (S_WV, 118)):
            kopf.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
            self.tabelle.setColumnWidth(c, breite)

        self.tabelle.selectionModel().selectionChanged.connect(self.auswahl_geaendert)
        self.tabelle.doubleClicked.connect(self.extern_oeffnen)
        return self.tabelle

    def _baue_detail(self) -> QWidget:
        behaelter = QWidget()
        lay = QVBoxLayout(behaelter)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.titel = QLabel("Kein Lead ausgewählt")
        self.titel.setStyleSheet("font-size: 17px; font-weight: 600;")
        lay.addWidget(self.titel)

        self.untertitel = QLabel("")
        self.untertitel.setStyleSheet(f"color: {MUTED};")
        self.untertitel.setOpenExternalLinks(True)
        lay.addWidget(self.untertitel)

        self.tabs = QTabWidget()

        self.befund_ansicht = QTextBrowser()
        self.befund_ansicht.setOpenExternalLinks(True)
        self.tabs.addTab(self.befund_ansicht, "Befunde")

        # Der Browser wird erst beim ersten Blick auf den Tab erzeugt:
        # QWebEngineView startet eine Chromium-Instanz, und wer nur Befunde
        # sichtet, braucht sie nie.
        self.browser = None
        self._browser_fehlt = False
        self.web_platz = QWidget()
        QVBoxLayout(self.web_platz).setContentsMargins(0, 0, 0, 0)
        self.tabs.addTab(self.web_platz, "Vorschau")
        self.tabs.setTabToolTip(1, "Schneller Blick ohne Fensterwechsel (Chromium)")
        self.tabs.currentChanged.connect(self.tab_gewechselt)

        lay.addWidget(self.tabs, 1)
        lay.addWidget(self._baue_statusfeld())
        return behaelter

    def _baue_statusfeld(self) -> QWidget:
        kasten = QWidget()
        kasten.setObjectName("statusfeld")
        lay = QVBoxLayout(kasten)
        lay.setContentsMargins(12, 10, 12, 12)

        kopf = QLabel("AKQUISE")
        kopf.setStyleSheet(f"color: {MUTED}; font-size: 11px; letter-spacing: 1px;")
        lay.addWidget(kopf)

        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 0)
        form.setSpacing(7)

        zeile = QHBoxLayout()
        self.ed_status = QComboBox()
        self.ed_status.addItems(audit.STATUS_WERTE)
        self.ed_status.currentIndexChanged.connect(self.status_speichern)
        zeile.addWidget(self.ed_status, 1)

        self.chk_wv = Schalter("Wiedervorlage")
        self.chk_wv.toggled.connect(self.wv_umgeschaltet)
        zeile.addWidget(self.chk_wv)

        self.ed_wv = QDateEdit()
        self.ed_wv.setCalendarPopup(True)
        self.ed_wv.setDisplayFormat("dd.MM.yyyy")
        self.ed_wv.setDate(QDate.currentDate().addDays(14))
        self.ed_wv.setEnabled(False)
        self.ed_wv.dateChanged.connect(self.status_speichern)
        zeile.addWidget(self.ed_wv)
        form.addRow("Status", zeile)

        self.ed_notiz = QPlainTextEdit()
        self.ed_notiz.setPlaceholderText("Notiz zum Gespräch …")
        self.ed_notiz.setFixedHeight(70)
        self.ed_notiz.focusOutEvent = self._notiz_fokus_weg(self.ed_notiz.focusOutEvent)
        form.addRow("Notiz", self.ed_notiz)
        lay.addLayout(form)

        knoepfe = QHBoxLayout()
        knoepfe.addStretch(1)
        self.btn_pdf = QPushButton("One-Pager als PDF …")
        self.btn_pdf.clicked.connect(self.pdf_exportieren)
        knoepfe.addWidget(self.btn_pdf)
        self.btn_web = QPushButton("Im Browser öffnen")
        self.btn_web.setToolTip(
            "Öffnet die Seite im Standardbrowser des Systems.\n"
            "Doppelklick auf eine Zeile tut dasselbe."
        )
        self.btn_web.clicked.connect(self.extern_oeffnen)
        knoepfe.addWidget(self.btn_web)
        lay.addLayout(knoepfe)

        self.statusfeld = kasten
        kasten.setEnabled(False)
        return kasten

    def _notiz_fokus_weg(self, original):
        def handler(event):
            original(event)
            self.status_speichern()
        return handler

    def _baue_menue(self) -> None:
        datei = self.menuBar().addMenu("&Datei")
        for text, kurz, fn in (
            ("CSV öffnen …", QKeySequence.StandardKey.Open, self.csv_dialog),
            ("Bericht exportieren …", QKeySequence("Ctrl+E"), self.bericht_exportieren),
        ):
            a = QAction(text, self)
            a.setShortcut(kurz)
            a.triggered.connect(fn)
            datei.addAction(a)
        datei.addSeparator()
        a = QAction("Beenden", self)
        a.setShortcut(QKeySequence.StandardKey.Quit)
        a.triggered.connect(self.close)
        datei.addAction(a)

        lauf = self.menuBar().addMenu("&Lauf")
        a = QAction("Prüfen starten", self)
        a.setShortcut(QKeySequence("Ctrl+R"))
        a.triggered.connect(self.lauf_starten)
        lauf.addAction(a)

    # --- Daten -------------------------------------------------------------

    def csv_dialog(self) -> None:
        pfad, _ = QFileDialog.getOpenFileName(self, "Leads-CSV öffnen", "", "CSV (*.csv);;Alle Dateien (*)")
        if pfad:
            self.csv_laden(Path(pfad))

    def csv_laden(self, pfad: Path) -> None:
        try:
            self.leads = audit.read_leads(pfad)
        except SystemExit as exc:
            QMessageBox.warning(self, "CSV passt nicht", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "CSV konnte nicht gelesen werden", str(exc))
            return
        self.csv_pfad = pfad
        self.setWindowTitle(f"site-audit — {pfad.name}")
        self.aus_cache_laden()
        self.statusBar().showMessage(
            f"{len(self.leads)} Leads aus {pfad.name} geladen. Noch nicht geprüfte Seiten mit „Prüfen“ abrufen."
        )

    def aus_cache_laden(self) -> None:
        """Zeigt, was schon im Cache liegt, damit das Fenster nie leer startet."""
        cache = Cache(self.db_pfad)
        gefunden: list[Result] = []
        try:
            for lead in self.leads:
                res = cache.get(lead, 0, False)
                if res is not None:
                    gefunden.append(res)
            if not self.leads:
                for (payload,) in cache.conn.execute("SELECT payload FROM audit").fetchall():
                    try:
                        gefunden.append(Result(**audit.json.loads(payload)))
                    except Exception:  # noqa: BLE001 - einzelner kaputter Eintrag darf nicht stoeren
                        continue
        finally:
            cache.close()
        self.ergebnisse_setzen(gefunden)

    def ergebnisse_setzen(self, ergebnisse: list[Result]) -> None:
        alle = self.statusbuch.alle()
        zeilen = [
            Zeile(r, alle.get(Statusbuch.key(r.url), LeadStatus(url_key=Statusbuch.key(r.url))))
            for r in ergebnisse
        ]
        zeilen.sort(key=lambda z: (-z.result.score, z.result.name.lower()))
        self.model.setzen(zeilen)
        self._filter_listen_fuellen(ergebnisse)
        self.filter_anwenden()
        if zeilen and not self.tabelle.selectionModel().hasSelection():
            self.tabelle.selectRow(0)

    def _filter_listen_fuellen(self, ergebnisse: list[Result]) -> None:
        for box, werte in (
            (self.f_ort, sorted({r.ort for r in ergebnisse if r.ort})),
            (self.f_branche, sorted({r.branche for r in ergebnisse if r.branche})),
        ):
            vorher = box.currentData()
            box.blockSignals(True)
            box.clear()
            box.addItem("alle Orte" if box is self.f_ort else "alle Branchen", "")
            for w in werte:
                box.addItem(w, w)
            idx = box.findData(vorher)
            box.setCurrentIndex(idx if idx >= 0 else 0)
            box.blockSignals(False)

    # --- Filter ------------------------------------------------------------

    def filter_anwenden(self) -> None:
        self.proxy.ort = self.f_ort.currentData() or ""
        self.proxy.branche = self.f_branche.currentData() or ""
        self.proxy.akquise = self.f_akquise.currentData() or ""
        self.proxy.min_score = float(self.f_min.currentData() or 0)
        self.proxy.nur_faellig = self.chk_faellig.isChecked()
        self.proxy.suche = self.f_suche.text().strip().lower()
        self.proxy.invalidateRowsFilter()
        self.lbl_anzahl.setText(f"{self.proxy.rowCount()} von {self.model.rowCount()}")

    # --- Auswahl und Detail ------------------------------------------------

    def aktuelle_zeile(self) -> Zeile | None:
        idx = self.tabelle.currentIndex()
        if not idx.isValid():
            return None
        return self.model.zeile(self.proxy.mapToSource(idx).row())

    def auswahl_geaendert(self) -> None:
        z = self.aktuelle_zeile()
        if z is None:
            self.statusfeld.setEnabled(False)
            self.titel.setText("Kein Lead ausgewählt")
            self.untertitel.setText("")
            self.befund_ansicht.setHtml("")
            return

        r, st = z.result, z.status
        self.titel.setText(r.name)
        ziel = r.final_url or ("https://" + r.url)
        teile = [f'<a href="{ziel}" style="color:{ACCENT};">{ziel}</a>']
        if r.ort:
            teile.append(r.ort)
        if r.branche:
            teile.append(r.branche)
        if r.cms:
            teile.append(r.cms)
        if r.pagespeed is not None:
            teile.append(f"PageSpeed {r.pagespeed}")
        self.untertitel.setText("  ·  ".join(teile))
        self.befund_ansicht.setHtml(self._befund_html(r))

        self._lade_sperre = True
        self.statusfeld.setEnabled(True)
        idx = self.ed_status.findText(st.status)
        self.ed_status.setCurrentIndex(idx if idx >= 0 else 0)
        self.ed_notiz.setPlainText(st.notiz)
        hat_wv = bool(st.wiedervorlage)
        self.chk_wv.sofort_setzen(hat_wv)
        self.ed_wv.setEnabled(hat_wv)
        # Ohne eigenes Datum auf den Vorschlag zurueck, sonst steht beim
        # naechsten Lead noch das Datum des vorigen im Feld.
        self.ed_wv.setDate(
            QDate.fromString(st.wiedervorlage, "yyyy-MM-dd")
            if hat_wv
            else QDate.currentDate().addDays(14)
        )
        self._lade_sperre = False

        if self.tabs.currentIndex() == 1:
            browser = self._browser_sicherstellen()
            if browser is not None:
                browser.setUrl(QUrl(ziel))

    def _browser_sicherstellen(self):
        """QWebEngineView erst bei Bedarf erzeugen.

        Die Vorschau ist immer Chromium - Qt kann Firefox nicht einbetten,
        Mozilla hat die Einbettungs-API fuer Gecko eingestellt. Wer das nicht
        braucht, kann PySide6-Essentials statt pyside6 installieren; dann
        fehlt das Modul und dieser Tab erklaert sich selbst, statt zu kippen.
        """
        if self.browser is None and not self._browser_fehlt:
            try:
                from PySide6.QtWebEngineWidgets import QWebEngineView
            except ImportError:
                self._browser_fehlt = True
                hinweis = QLabel(
                    "Die eingebettete Vorschau braucht QtWebEngine, das hier nicht "
                    "installiert ist.\n\nMit „Im Browser öffnen“ oder einem Doppelklick "
                    "auf die Zeile geht die Seite\nim Standardbrowser des Systems auf."
                )
                hinweis.setAlignment(Qt.AlignmentFlag.AlignCenter)
                hinweis.setStyleSheet(f"color: {MUTED};")
                self.web_platz.layout().addWidget(hinweis)
                return None
            self.browser = QWebEngineView()
            self.web_platz.layout().addWidget(self.browser)
        return self.browser

    def tab_gewechselt(self, index: int) -> None:
        if index != 1:
            return
        z = self.aktuelle_zeile()
        if z is None:
            return
        ziel = z.result.final_url or ("https://" + z.result.url)
        browser = self._browser_sicherstellen()
        if browser is not None and browser.url().toString() != ziel:
            browser.setUrl(QUrl(ziel))

    def _befund_html(self, r: Result) -> str:
        farbe = score_farbe(r.score, r.status)
        teile = [
            f'<body style="background:{PANEL}; color:{FG}; font-family:sans-serif; font-size:14px;">',
            f'<p style="font-size:30px; font-weight:700; color:{farbe}; margin:0 0 2px;">{r.score:.0f}'
            f'<span style="font-size:13px; color:{MUTED}; font-weight:400;"> / 100 Chaos-Score</span></p>',
        ]
        if r.argumente:
            teile.append(
                f'<p style="color:{MUTED}; font-size:11px; letter-spacing:1px; margin:16px 0 4px;">'
                "ARGUMENTE FÜR DAS ERSTGESPRÄCH</p><ul style='margin:0 0 4px; padding-left:18px;'>"
            )
            teile += [f'<li style="margin-bottom:5px;">{self._esc(a)}</li>' for a in r.argumente]
            teile.append("</ul>")
        if r.befunde:
            teile.append(
                f'<p style="color:{MUTED}; font-size:11px; letter-spacing:1px; margin:18px 0 4px;">'
                f"BEFUNDE ({len(r.befunde)})</p><ul style='margin:0; padding-left:18px; color:#c3cdda;'>"
            )
            teile += [f'<li style="margin-bottom:4px;">{self._esc(b)}</li>' for b in r.befunde]
            teile.append("</ul>")
        teile.append(
            f'<p style="color:{MUTED}; font-size:11px; margin-top:20px;">Geprüft am '
            f"{r.geprueft_am.replace('T', ' um ')}. Rechtliche Hinweise sind keine Rechtsauskunft.</p></body>"
        )
        return "".join(teile)

    @staticmethod
    def _esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # --- Status pflegen ----------------------------------------------------

    def wv_umgeschaltet(self) -> None:
        self.ed_wv.setEnabled(self.chk_wv.isChecked())
        self.status_speichern()

    def status_speichern(self) -> None:
        if self._lade_sperre:
            return
        z = self.aktuelle_zeile()
        if z is None:
            return
        wv = self.ed_wv.date().toString("yyyy-MM-dd") if self.chk_wv.isChecked() else ""
        neu = self.statusbuch.setzen(
            z.result.url, self.ed_status.currentText(), self.ed_notiz.toPlainText().strip(), wv
        )
        quelle = self.proxy.mapToSource(self.tabelle.currentIndex())
        self.model.status_aktualisieren(quelle.row(), neu)
        self.proxy.invalidateRowsFilter()
        self.statusBar().showMessage(f"Status gespeichert: {z.result.name} → {neu.status}", 4000)

    # --- Lauf --------------------------------------------------------------

    def lauf_starten(self) -> None:
        if self.lauf is not None and self.lauf.isRunning():
            return
        if not self.leads:
            QMessageBox.information(self, "Keine Leads", "Erst eine Leads-CSV öffnen.")
            return

        args = argparse.Namespace(
            pagespeed=self.chk_psi.isChecked() and self.psi_key is not None,
            refresh=self.chk_refresh.isChecked(),
            cache=self.db_pfad,
            no_cache=False,
            max_age=0,
            concurrency=audit.MAX_PARALLEL,
            host_interval=audit.HOST_INTERVAL,
            assets=audit.DEFAULT_ASSETS,
            timeout=audit.DEFAULT_TIMEOUT,
        )

        self.lauf = LaufThread(self.leads, args, self.psi_key)
        self.lauf.fortschritt.connect(self.lauf_fortschritt)
        self.lauf.fertig.connect(self.lauf_fertig)
        self.lauf.fehlgeschlagen.connect(self.lauf_fehler)
        self.lauf.finished.connect(self.lauf_aufraeumen)

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_oeffnen.setEnabled(False)
        self.fortschritt.setVisible(True)
        self.fortschritt.setRange(0, len(self.leads))
        self.fortschritt.setValue(0)
        self.statusBar().showMessage(f"Prüfe {len(self.leads)} Seiten …")
        self.lauf.start()

    def lauf_stoppen(self) -> None:
        if self.lauf is not None:
            self.lauf.stoppen()
            self.statusBar().showMessage("Abbruch angefordert, laufende Abrufe werden zu Ende geführt …")
            self.btn_stop.setEnabled(False)

    def lauf_fortschritt(self, fertig: int, gesamt: int, res: Result, aus_cache: bool) -> None:
        self.fortschritt.setValue(fertig)
        woher = "aus Cache" if aus_cache else f"Score {res.score:.0f}"
        self.statusBar().showMessage(f"[{fertig}/{gesamt}]  {res.name} — {woher}")

    def lauf_fertig(self, ergebnisse: list[Result]) -> None:
        self.ergebnisse_setzen(ergebnisse)
        self.statusBar().showMessage(f"Fertig: {len(ergebnisse)} Seiten geprüft.", 8000)

    def lauf_fehler(self, text: str) -> None:
        QMessageBox.warning(self, "Lauf abgebrochen", text)
        self.statusBar().showMessage("Lauf abgebrochen.", 8000)

    def lauf_aufraeumen(self) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_oeffnen.setEnabled(True)
        self.fortschritt.setVisible(False)
        self.lauf = None

    # --- Export ------------------------------------------------------------

    def bericht_exportieren(self) -> None:
        if not self.model.zeilen:
            return
        basis, _ = QFileDialog.getSaveFileName(self, "Bericht speichern", "ergebnis", "Basisname (*)")
        if not basis:
            return
        basis = basis.removesuffix(".csv").removesuffix(".html")
        ergebnisse = [z.result for z in self.model.zeilen]
        status = self.statusbuch.alle()
        audit.write_csv(ergebnisse, Path(basis + ".csv"), status)
        audit.write_html(ergebnisse, Path(basis + ".html"), str(self.csv_pfad or "GUI"), status)
        self.statusBar().showMessage(f"Geschrieben: {basis}.csv und {basis}.html", 8000)

    def pdf_exportieren(self) -> None:
        z = self.aktuelle_zeile()
        if z is None:
            return
        vorschlag = "".join(c for c in z.result.name if c.isalnum() or c in " -_").strip() or "lead"
        pfad, _ = QFileDialog.getSaveFileName(self, "One-Pager speichern", f"{vorschlag}.pdf", "PDF (*.pdf)")
        if not pfad:
            return

        drucker = QPrinter(QPrinter.PrinterMode.HighResolution)
        drucker.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        drucker.setOutputFileName(pfad)
        dok = QTextDocument()
        dok.setHtml(self._pdf_html(z))
        dok.setPageSize(drucker.pageRect(QPrinter.Unit.Point).size())
        dok.print_(drucker)
        self.statusBar().showMessage(f"One-Pager geschrieben: {pfad}", 8000)

    def _pdf_html(self, z: Zeile) -> str:
        r, st = z.result, z.status
        heute = datetime.now().strftime("%d.%m.%Y")
        arg = "".join(f"<li>{self._esc(a)}</li>" for a in r.argumente) or "<li>–</li>"
        bef = "".join(f"<li>{self._esc(b)}</li>" for b in r.befunde) or "<li>–</li>"
        notiz = f"<p><b>Notiz:</b> {self._esc(st.notiz)}</p>" if st.notiz else ""
        wv = f"<p><b>Wiedervorlage:</b> {st.wiedervorlage}</p>" if st.wiedervorlage else ""
        return f"""
        <h1 style="margin-bottom:0;">{self._esc(r.name)}</h1>
        <p style="color:#555; margin-top:2px;">
          {self._esc(r.final_url or r.url)}<br>
          {self._esc(r.ort)} &middot; {self._esc(r.branche)} &middot; {self._esc(r.cms or "Technik unbekannt")}
        </p>
        <p style="font-size:15pt;"><b>Chaos-Score {r.score:.0f} / 100</b>
          &nbsp;<span style="color:#555;">(höher = mehr Relaunch-Bedarf)</span>
          {f"&nbsp;&middot;&nbsp; PageSpeed mobil {r.pagespeed}/100" if r.pagespeed is not None else ""}
        </p>
        <h3>Argumente für das Gespräch</h3><ul>{arg}</ul>
        <h3>Alle Befunde</h3><ul>{bef}</ul>
        <h3>Akquise</h3><p><b>Status:</b> {self._esc(st.status)}</p>{wv}{notiz}
        <p>&nbsp;</p><p>Gesprächsnotizen:</p>
        <p>______________________________________________________________</p>
        <p>______________________________________________________________</p>
        <p>______________________________________________________________</p>
        <hr>
        <p style="font-size:8pt; color:#777;">
          Automatisch erzeugt von site-audit am {heute}. Geprüft am
          {r.geprueft_am.replace("T", " um ")}. Rechtliche Punkte sind Hinweise
          zur Prüfung, keine Rechtsauskunft.
        </p>"""

    # --- Sonstiges ---------------------------------------------------------

    def extern_oeffnen(self) -> None:
        """Im Standardbrowser des Systems oeffnen, nicht im eingebetteten."""
        z = self.aktuelle_zeile()
        if z is not None:
            QDesktopServices.openUrl(QUrl(z.result.final_url or ("https://" + z.result.url)))

    def closeEvent(self, event) -> None:
        if self.lauf is not None and self.lauf.isRunning():
            self.lauf.stoppen()
            self.lauf.wait(5000)
        self.statusbuch.close()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

STYLE = f"""
QWidget {{ background: {BG}; color: {FG}; font-size: 14px; }}
QMenuBar, QMenu {{ background: {PANEL}; }}
QMenu::item:selected, QMenuBar::item:selected {{ background: {PANEL2}; }}
QTableView {{
    background: {PANEL}; alternate-background-color: #141a23;
    gridline-color: {LINE}; border: 1px solid {LINE}; border-radius: 8px;
    selection-background-color: #24344a; selection-color: {FG};
}}
QHeaderView::section {{
    background: {PANEL2}; color: {MUTED}; border: none;
    border-bottom: 1px solid {LINE}; padding: 7px 9px; font-size: 12px;
}}
QTextBrowser, QPlainTextEdit, QLineEdit, QComboBox, QDateEdit {{
    background: {PANEL2}; border: 1px solid {LINE}; border-radius: 7px; padding: 5px 8px;
}}
QComboBox QAbstractItemView {{ background: {PANEL2}; selection-background-color: #24344a; }}
QTextBrowser {{ padding: 12px; }}
QPushButton {{
    background: {PANEL2}; border: 1px solid {LINE}; border-radius: 7px; padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #5a6676; border-color: #222a36; }}
QPushButton#start {{ background: #1d3b57; border-color: #2d5c86; font-weight: 600; }}
QPushButton#start:hover {{ background: #24496b; }}
QWidget#statusfeld {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 8px; }}
QTabBar::tab {{
    background: transparent; color: {MUTED}; padding: 7px 16px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {FG}; border-bottom: 2px solid {ACCENT}; }}
QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 8px; background: {PANEL}; }}
QProgressBar {{
    background: {PANEL2}; border: 1px solid {LINE}; border-radius: 6px;
    text-align: center; height: 16px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QStatusBar {{ color: {MUTED}; }}

QSplitter::handle {{ background: transparent; width: 8px; }}
QScrollBar:vertical {{ background: transparent; width: 11px; }}
QScrollBar::handle:vertical {{ background: #2f3a4a; border-radius: 5px; min-height: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gui.py", description="site-audit mit grafischer Oberflaeche")
    p.add_argument("csv", nargs="?", type=Path, help="Leads-CSV, die beim Start geladen wird")
    p.add_argument("--cache", type=Path, default=Path(".site-audit-cache.sqlite"))
    args = p.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("site-audit")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)

    fenster = Fenster(args.csv, args.cache)
    fenster.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
