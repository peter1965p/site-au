#!/usr/bin/env python3
"""site-audit - Firmenwebseiten automatisiert auf Relaunch-Bedarf pruefen.

Aufruf:  python audit.py leads.csv --out ergebnis
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sqlite3
import ssl
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

import kontaktsuche
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from selectolax.lexbor import LexborHTMLParser

# ---------------------------------------------------------------------------
# GEWICHTE - hier justieren. Hoeherer Wert = der Check schlaegt staerker auf
# den Chaos-Score durch. Die Zahlen sind relativ zueinander zu lesen, der
# Score wird am Ende auf 0-100 normiert.
# ---------------------------------------------------------------------------

WEIGHTS: dict[str, float] = {
    "substanz": 20.0,  # Platzhalterseite oder Seite praktisch ohne Inhalt
    "erreichbarkeit": 12.0,  # Timeout, HTTP-Fehler, lange Redirect-Ketten
    "https": 14.0,  # kein TLS, kaputtes Zertifikat, Mixed Content
    "mobil": 18.0,  # Viewport-Meta fehlt / Zoom gesperrt
    "aktualitaet": 9.0,  # altes Copyright-Jahr, alter Last-Modified
    "technologie": 16.0,  # Baukasten, altes CMS, Flash/Frames/Tabellenlayout
    "recht": 15.0,  # Impressum, Datenschutz, Cookies, Google Fonts
    "seo": 9.0,  # Title, Description, H1, alt-Texte
    "bilder": 6.0,  # Seitengewicht, Riesenbilder, Mini-Bilder
    "kontakt": 6.0,  # kein tel:-Link, kein Kontaktformular
    "konsistenz": 3.0,  # widerspruechliche Telefonnummern
    # Bewusst niedrig: PSI misst Geschwindigkeit, nicht Zeitgemaessheit.
    # Alte Baukastenseiten sind klein und dadurch schnell, moderne Seiten
    # mit viel Bildmaterial langsam - gemessen an unseren Testseiten
    # korreliert ein hoher PSI-Wert mit +0,82 zum Chaos-Score. Ein hohes
    # Gewicht wuerde die besten Leads nach unten ziehen.
    "pagespeed": 5.0,  # nur mit --pagespeed und PSI_KEY
}

# Ab dieser gewichteten Punktsumme gilt eine Seite als Totalschaden (Score 100).
# Die volle Gewichtssumme als Bezug zu nehmen waere zwar sauber gerechnet, aber
# praktisch unbrauchbar: keine reale Seite faellt in allen elf Kategorien
# gleichzeitig durch, dadurch landet selbst eine ruinierte Seite nur bei ~45.
# 75 entspricht "in den vier bis fuenf schwersten Kategorien komplett durchgefallen".
# Kleiner = die Skala spreizt staerker nach oben, groesser = milder.
SCORE_REFERENZ = 75.0

USER_AGENT = "site-audit/1.0 (+paeffgen-it.de)"
ROBOTS_AGENT = "site-audit"

# Gleichzeitig gepruefte Seiten. Die Hoeflichkeit gegenueber den Seiten regelt
# HOST_INTERVAL (1 Request pro Sekunde und Host), nicht diese Zahl: Jede Seite
# gehoert einem anderen Host, mehr parallele Firmen belasten also keinen Host
# staerker. Eine Firma braucht rund 10 s, fast alles davon ist Warten auf den
# Takt - mit 10 Plaetzen ergibt das nur ~35 Firmen pro Minute.
MAX_PARALLEL = 30
HOST_INTERVAL = 1.0  # Sekunden Mindestabstand zwischen Requests pro Host
DEFAULT_TIMEOUT = 20.0
CONNECT_TIMEOUT = 8.0  # Verbindungsaufbau; wer in 8 s nicht antwortet, antwortet meist gar nicht
TIMEOUT_VERSUCHE = 2   # bei Zeitueberschreitung nur einmal wiederholen
RETRIES = 3            # Versuche pro Kandidaten-URL bei sonstigen Netzfehlern
RETRY_BACKOFF = 1.5    # Sekunden, waechst mit jedem Fehlversuch
DEFAULT_ASSETS = 6  # wie viele Bilder/CSS/JS pro Seite vermessen werden (Inhaltsbilder zuerst)
ASSET_PEEK = 32 * 1024  # so viele Bytes pro Asset lesen wir maximal

CURRENT_YEAR = datetime.now().year
STALE_YEAR = 2022  # Copyright aelter als das gilt als veraltet
TITLE_MAX = 65
DESC_MAX = 165
SMALL_IMAGE_PX = 400
HUGE_IMAGE_BYTES = 1_500_000

console = Console()


# ---------------------------------------------------------------------------
# Datenstrukturen
# ---------------------------------------------------------------------------


@dataclass
class Lead:
    name: str
    url: str
    ort: str = ""
    branche: str = ""
    telefon: str = ""  # falls die Quelle schon welche mitbringt
    email: str = ""


@dataclass
class Finding:
    """Ein Klartext-Befund. `argument` ist der Satz fuers Erstgespraech."""

    text: str
    argument: str | None = None


@dataclass
class CheckResult:
    key: str
    severity: float | None  # None = Check konnte nicht laufen
    findings: list[Finding] = field(default_factory=list)


@dataclass
class Fetched:
    url: str
    final_url: str = ""
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    html: str = ""
    html_bytes: int = 0
    redirects: int = 0
    elapsed: float = 0.0
    error: str | None = None
    tls_problem: str | None = None
    scheme_downgrade: bool = False  # https ging nicht, http schon


@dataclass
class AssetInfo:
    refs: int = 0
    measured: int = 0
    total_bytes: int = 0
    oversized: list[tuple[str, int]] = field(default_factory=list)
    images_measured: int = 0  # nur Inhaltsbilder mit bekannter Breite
    images_small: int = 0  # davon schmaler als SMALL_IMAGE_PX


@dataclass
class LegalInfo:
    impressum_url: str | None = None
    impressum_status: int | None = None
    impressum_html: str = ""
    datenschutz_url: str | None = None
    datenschutz_status: int | None = None


@dataclass
class Context:
    """Alles, was die Checks brauchen. Ohne Netz konstruierbar -> testbar."""

    fetched: Fetched
    tree: LexborHTMLParser | None
    assets: AssetInfo | None = None
    legal: LegalInfo | None = None
    psi_score: int | None = None
    psi_ran: bool = False


STATUS_OK = "ok"
STATUS_UNERREICHBAR = "nicht erreichbar"
STATUS_PRUEFFEHLER = "pruefung fehlgeschlagen"  # eigener Fehler, nicht der der Seite

GRUND_TEXT = {
    "timeout": "Zeitueberschreitung",
    "dns": "Adresse nicht aufloesbar",
    "verbindung": "Verbindung fehlgeschlagen",
    "http5xx": "Serverfehler (HTTP 5xx)",
    "weiterleitungen": "zu viele Weiterleitungen",
    "robots": "per robots.txt fuer automatische Pruefung gesperrt",
}


def grund_aus_text(text: str) -> str:
    """Grund einer Nichterreichbarkeit aus einer Fehlermeldung ableiten."""
    t = text.lower()
    if "timeout" in t or "zeitueberschreitung" in t:
        return "timeout"
    if any(k in t for k in ("name or service not known", "nodename nor servname", "no address associated",
                            "name resolution", "errno -2", "errno -5", "gaierror")):
        return "dns"
    if "weiterleitungen" in t or "redirect" in t:
        return "weiterleitungen"
    if "http 5" in t:
        return "http5xx"
    return "verbindung"


def ist_dauerhaft_tot(text: str) -> bool:
    """Die Adresse gibt es nicht (DNS). Wiederholen und http bringen nichts.

    Bewusst nur die endgueltigen Faelle. "Temporary failure in name resolution"
    (Errno -3) ist ein Aussetzer und darf wiederholt werden.
    """
    t = text.lower()
    return any(
        k in t for k in ("name or service not known", "nodename nor servname", "no address associated", "errno -2", "errno -5")
    )


@dataclass
class Result:
    name: str
    url: str
    ort: str
    branche: str
    final_url: str
    status: str  # ok | nicht erreichbar | pruefung fehlgeschlagen
    score: float | None  # None = nicht bewertet (Seite nicht erreichbar)
    cms: str
    argumente: list[str]
    befunde: list[str]
    severities: dict[str, float | None]
    geprueft_am: str
    pagespeed: int | None = None
    telefon: str = ""  # von der Seite oder aus der Quelle
    email: str = ""
    kontakt_quelle: str = ""  # Startseite | Impressum | OpenStreetMap
    kontaktformular: bool = False
    empfehlung: str = ""  # Anruf | Mail | Kontaktformular | ...
    empfehlung_grund: str = ""
    unerreichbar_grund: str = ""  # timeout | dns | verbindung | http5xx | weiterleitungen | robots

    def __post_init__(self) -> None:
        """Altbestand aus dem Cache auf die neuen Status umstellen.

        Bis zur Umstellung hiessen diese Faelle "fehler" und "gesperrt" und
        trugen einen Score (100 bzw. 0). Wer den Cache liest, soll dasselbe
        sehen wie nach einem Neulauf - ohne dass tausende Eintraege neu
        geprueft werden muessen.
        """
        if self.status == "gesperrt":
            self.status, self.unerreichbar_grund = STATUS_UNERREICHBAR, "robots"
        elif self.status == "fehler":
            if any("[Pruefung]" in b for b in self.befunde):
                self.status = STATUS_PRUEFFEHLER
            else:
                self.status = STATUS_UNERREICHBAR
                self.unerreichbar_grund = self.unerreichbar_grund or grund_aus_text(" ".join(self.befunde))
        elif self.status == "ok" and any("Server antwortet mit HTTP 5" in b for b in self.befunde):
            self.status, self.unerreichbar_grund = STATUS_UNERREICHBAR, "http5xx"
        else:
            return
        self.score = None
        self.empfehlung, self.empfehlung_grund = naechster_schritt(self)

    @property
    def bewertet(self) -> bool:
        """Hat einen Chaos-Score. Nicht erreichbare Seiten haben keinen."""
        return self.score is not None


# ---------------------------------------------------------------------------
# Kleine Helfer
# ---------------------------------------------------------------------------


def combine(values: list[float]) -> float:
    """Mehrere Signale eines Checks zu einer Severity 0..1 verrechnen.

    Das staerkste Signal gibt den Ton an, jedes weitere legt ein Viertel
    seiner eigenen Severity obendrauf. So dominiert ein schwerer Mangel,
    aber eine Haeufung kleiner Maengel kommt trotzdem oben an.
    """
    vals = sorted((v for v in values if v > 0), reverse=True)
    if not vals:
        return 0.0
    sev = vals[0]
    for v in vals[1:]:
        sev += v * 0.25
    return round(min(1.0, sev), 4)


def normalize_url(raw: str) -> list[str]:
    """Kandidaten-URLs. Ohne Schema erst https, dann http probieren."""
    raw = raw.strip()
    if not raw:
        return []
    if "://" in raw:
        return [raw]
    return [f"https://{raw}", f"http://{raw}"]


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def ssl_reason(exc: BaseException) -> str | None:
    """Im Exception-Chain nach einem TLS-Problem suchen."""
    seen: BaseException | None = exc
    for _ in range(10):
        if seen is None:
            break
        if isinstance(seen, ssl.SSLCertVerificationError):
            return getattr(seen, "verify_message", None) or str(seen)
        if isinstance(seen, ssl.SSLError):
            return str(seen)
        seen = seen.__cause__ or seen.__context__
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate" in text.lower():
        return text
    return None


def meta_content(tree: LexborHTMLParser, name: str) -> str | None:
    """Meta-Tag per name= holen, Gross-/Kleinschreibung egal."""
    for node in tree.css("meta"):
        attrs = node.attributes
        got = (attrs.get("name") or attrs.get("property") or "").strip().lower()
        if got == name.lower():
            return (attrs.get("content") or "").strip()
    return None


def visible_text(tree: LexborHTMLParser) -> str:
    body = tree.body
    if body is None:
        return ""
    for node in body.css("script, style, noscript, template"):
        node.decompose()
    return body.text(separator=" ", strip=True)


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Pixelmasse aus den ersten Bytes einer Bilddatei lesen.

    Unterstuetzt PNG, GIF, JPEG, WebP und SVG. Rein, damit testbar.
    """
    if len(data) < 16:
        return None

    # PNG
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)

    # GIF
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return int(w), int(h)

    # WebP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h
        if chunk == b"VP8 " and len(data) >= 30:
            w = int.from_bytes(data[26:28], "little") & 0x3FFF
            h = int.from_bytes(data[28:30], "little") & 0x3FFF
            return w, h
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        return None

    # JPEG
    if data[:2] == b"\xff\xd8":
        i = 2
        end = len(data)
        while i + 9 < end:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if i + 4 > end:
                break
            seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 9 <= end:
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return int(w), int(h)
                break
            i += 2 + seg_len
        return None

    # SVG
    head = data[:2048].decode("utf-8", "ignore")
    if "<svg" in head:
        vb = re.search(r'viewBox\s*=\s*["\']\s*[\d.+-]+[,\s]+[\d.+-]+[,\s]+([\d.]+)[,\s]+([\d.]+)', head)
        if vb:
            return int(float(vb.group(1))), int(float(vb.group(2)))
        w = re.search(r'\bwidth\s*=\s*["\']\s*([\d.]+)', head)
        h = re.search(r'\bheight\s*=\s*["\']\s*([\d.]+)', head)
        if w and h:
            return int(float(w.group(1))), int(float(h.group(1)))
    return None


PHONE_RE = re.compile(
    r"(?:\+49|0049|\(0\)|\b0)[\s/.\-()]*\d[\d\s/.\-()]{6,20}\d"
)


def normalize_phone(raw: str) -> str | None:
    """Deutsche Telefonnummer auf reine Ziffern mit Laendervorwahl bringen."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0049"):
        digits = "49" + digits[4:]
    elif digits.startswith("49") and len(digits) >= 11:
        pass
    elif digits.startswith("0"):
        digits = "49" + digits[1:]
    # "+49 (0)6591 ..." ist eine gaengige Schreibweise. Nach der Laenderkennung
    # beginnt keine deutsche Vorwahl mit 0, die Null ist also immer die
    # weggelassene nationale Null.
    if digits.startswith("490"):
        digits = "49" + digits[3:]
    if len(digits) < 10 or len(digits) > 16:
        return None
    return digits


def find_phones(text: str) -> set[str]:
    out: set[str] = set()
    for m in PHONE_RE.finditer(text):
        norm = normalize_phone(m.group(0))
        if norm:
            out.add(norm)
    return out


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
# Adressen, die nie beim richtigen Ansprechpartner landen
EMAIL_MUELL = ("noreply", "no-reply", "postmaster", "abuse@", "webmaster@", "@sentry", "@example.")


def kontaktdaten(tree: LexborHTMLParser) -> tuple[str, str]:
    """Telefonnummer und E-Mail von der Seite lesen.

    Erst die verlinkten tel:/mailto:-Angaben, die sind eindeutig. Erst wenn
    es keine gibt, wird im sichtbaren Text gesucht.
    """
    telefon = email = ""
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        tief = href.lower()
        if not telefon and tief.startswith("tel:"):
            telefon = href[4:].strip()
        elif not email and tief.startswith("mailto:"):
            kandidat = href[7:].split("?")[0].strip()
            if kandidat and not any(m in kandidat.lower() for m in EMAIL_MUELL):
                email = kandidat
        if telefon and email:
            break

    if not telefon or not email:
        text = visible_text(tree)
        if not telefon:
            treffer = PHONE_RE.search(text)
            if treffer:
                telefon = " ".join(treffer.group(0).split())
        if not email:
            for m in EMAIL_RE.finditer(text):
                if not any(muell in m.group(0).lower() for muell in EMAIL_MUELL):
                    email = m.group(0)
                    break
    return telefon.strip(), email.strip()


# ---------------------------------------------------------------------------
# Naechster Schritt
# ---------------------------------------------------------------------------

# Ab hier lohnt die Ansprache ueberhaupt. Darunter ist die Seite in Ordnung
# und ein Relaunch waere schwer zu begruenden.
SCHWELLE_LOHNT = 30.0
SCHWELLE_DRINGEND = 60.0


def naechster_schritt(res: Result) -> tuple[str, str]:
    """Empfehlen, ob und wie der Lead angesprochen werden sollte.

    Zwei Fragen zugleich: Lohnt es sich (Score), und welcher Weg steht offen
    (Kontaktdaten). Ein hoher Score nuetzt nichts, wenn es keinen Draht gibt.
    """
    if res.status == STATUS_UNERREICHBAR:
        if res.unerreichbar_grund == "robots":
            return "manuell ansehen", "Die Seite verbietet die automatische Pruefung - von Hand ansehen."
        grund = GRUND_TEXT.get(res.unerreichbar_grund, "nicht erreichbar")
        # Bewusst kein "Anruf": eine tote Adresse stammt oft aus einem
        # Tippfehler in der Quelle, und ein Ausfall kann voruebergehend sein.
        # Wer deshalb anruft, steht mit einer falschen Behauptung da.
        return "manuell ansehen", f"{grund} - erst im Browser pruefen, ob die Adresse stimmt und die Seite wirklich fehlt."
    if res.status == STATUS_PRUEFFEHLER:
        return "manuell ansehen", "Die automatische Pruefung ist fehlgeschlagen."

    if res.severities.get("substanz") == 1.0:
        weg = "Nummer liegt vor" if res.telefon else "Kontaktweg muss noch gesucht werden"
        return (
            "Anruf" if res.telefon else "Kontakt suchen",
            f"Keine Website unter der Domain - der klarste Anlass ueberhaupt, {weg}. "
            "Vorher kurz pruefen, ob der Betrieb noch existiert: eine Platzhalterseite "
            "kann auch bedeuten, dass er aufgegeben hat.",
        )

    if res.score < SCHWELLE_LOHNT:
        return "zurueckstellen", f"Score {res.score:.0f} - die Seite ist in Ordnung, wenig Ansatzpunkte."

    dringend = res.score >= SCHWELLE_DRINGEND
    stufe = "Deutlicher Bedarf" if dringend else "Ansatzpunkte vorhanden"
    anzahl = len(res.argumente)

    if res.telefon:
        woher = f" (aus {res.kontakt_quelle})" if res.kontakt_quelle else ""
        return "Anruf", f"{stufe} (Score {res.score:.0f}), {anzahl} Argumente, Nummer liegt vor{woher}."
    if res.email:
        return "Mail", f"{stufe} (Score {res.score:.0f}), keine Telefonnummer, aber eine E-Mail-Adresse."
    if res.kontaktformular:
        return "Kontaktformular", f"{stufe} (Score {res.score:.0f}), nur ueber das Formular erreichbar."
    return "Kontakt suchen", f"{stufe} (Score {res.score:.0f}), auf der Startseite kein Kontaktweg zu finden."


def human_bytes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1000:
        return f"{n / 1000:.0f} kB"
    return f"{n} B"


# ---------------------------------------------------------------------------
# Checks - jeder nimmt den Context und liefert ein CheckResult
# ---------------------------------------------------------------------------


# Spuren von Platzhalter- und Parkseiten. Unter so einer Domain liegt keine
# Website - fuer die Akquise der beste Fall, aber nur, wenn das auch so
# benannt wird statt als "fehlender Viewport".
PARKSEITE_TEXT = (
    "diese domain ist bereits registriert",
    "diese domain wurde registriert",
    "domain ist reserviert",
    "diese webseite wird gerade erstellt",
    "website coming soon",
    "coming soon",
    "under construction",
    "im aufbau",
    "in kürze für sie da",
    "in kuerze fuer sie da",
    "hier entsteht eine neue",
    "hier entsteht die neue",
    "diese seite befindet sich im aufbau",
    # Platzhalter, wie sie Baukasten- und Hosting-Anbieter ausliefern
    "site in the making",
    "check back soon",
    "coming to this web address",
    "website im aufbau",
    "webseite im aufbau",
    "seite im aufbau",
    "bald online",
    "in kürze online",
    "in kuerze online",
    "demnächst online",
    "demnaechst online",
    "platzhalterseite",
    "default web page",
    "welcome to nginx",
    "apache2 debian default page",
    "it works!",
    "future home of something",
    "domain parking",
    "geparkte domain",
)
PARKSEITE_HOST = (
    "sedoparking.com", "parkingcrew.net", "above.com", "dan.com", "afternic.com",
    "bodis.com", "domainparking", "parklogic.com",
)
# Diese Anbieter liefern Parkseiten unter eigenem Namen aus
PARKSEITE_ANBIETER = ("ionos", "1&1", "strato", "godaddy", "hosteurope", "united-domains")

MIN_INHALT = 250  # Zeichen sichtbarer Text, darunter ist die Seite leer


def ist_parkseite(ctx: Context) -> str | None:
    """Erkennen, ob unter der Domain nur eine Platzhalterseite liegt.

    Rueckgabe ist der Grund im Klartext, sonst None. Bewusst streng: ein
    falscher Treffer wuerde eine echte Website als "nicht vorhanden" melden.
    """
    if ctx.tree is None:
        return None
    text = visible_text(ctx.tree)
    klein = text.lower()
    kurz = len(text) < 1200  # echte Firmenseiten sind laenger

    # Auch der Seitentitel zaehlt. Viele Platzhalter nennen ihr Stichwort nur dort
    # ("Under construction - Awesome site in the making!"), im sichtbaren Text
    # steht es dann in anderer Schreibweise oder gar nicht.
    titel = ctx.tree.css_first("title")
    titel_klein = titel.text(strip=True).lower() if titel else ""
    heu = klein + " " + titel_klein

    for marker in PARKSEITE_TEXT:
        if marker in heu and kurz:
            return "Platzhalterseite"

    ziel_host = (urlparse(ctx.fetched.final_url).hostname or "").lower()
    for host in PARKSEITE_HOST:
        if host in ziel_host:
            return "Domain-Parkdienst"

    # Anbietername plus Werbung fuer Baukaesten auf einer sehr kurzen Seite
    if kurz and any(a in klein for a in PARKSEITE_ANBIETER):
        if any(w in klein for w in ("domain", "webadresse", "mywebsite", "homepage-baukasten")):
            return "Platzhalterseite des Providers"
    return None


def check_substanz(ctx: Context) -> CheckResult:
    if ctx.tree is None:
        return CheckResult("substanz", None)

    grund = ist_parkseite(ctx)
    if grund:
        return CheckResult(
            "substanz",
            1.0,
            [
                Finding(
                    f"Unter der Domain liegt keine Website, nur eine {grund}",
                    "Die Firma hat eine Internetadresse, aber keine Website dahinter - "
                    "wer dort sucht, findet nichts.",
                )
            ],
        )

    # Weiterleitung auf ein soziales Netzwerk: die Firma hat keine eigene Seite
    ziel = (urlparse(ctx.fetched.final_url).hostname or "").lower()
    if any(ziel.endswith(h) or ziel.endswith("." + h) for h in SOCIAL_HOSTS):
        netz = next(h.split(".")[0].capitalize() for h in SOCIAL_HOSTS if h in ziel)
        return CheckResult(
            "substanz",
            1.0,
            [
                Finding(
                    f"Die Domain leitet auf {netz} weiter, es gibt keine eigene Website",
                    f"Die Firma hat statt einer Website nur eine {netz}-Seite - sie gehoert "
                    "damit niemandem ausser dem Netzwerk und taucht bei Google kaum auf.",
                )
            ],
        )

    laenge = len(visible_text(ctx.tree))
    if laenge >= MIN_INHALT:
        return CheckResult("substanz", 0.0)

    # Wenig Text im Quelltext heisst nicht wenig Inhalt: moderne Seiten bauen
    # ihn erst im Browser zusammen. Wer Titel, Beschreibung und Skripte
    # mitbringt, hat eine Seite - sie ist nur fuer Suchmaschinen schlechter
    # greifbar. Das als "kein Inhalt" zu melden waere ein Fehlalarm.
    titel = ctx.tree.css_first("title")
    hat_titel = bool(titel and titel.text(strip=True))
    hat_beschreibung = bool(meta_content(ctx.tree, "description"))
    skripte = len(ctx.tree.css("script[src], script[type=module]"))

    if hat_titel and hat_beschreibung and skripte:
        return CheckResult(
            "substanz",
            0.3,
            [
                Finding(
                    "Inhalte werden erst im Browser per JavaScript aufgebaut",
                    "Die Seiteninhalte entstehen erst im Browser - Suchmaschinen und "
                    "Vorschaubilder in sozialen Netzwerken sehen davon wenig.",
                )
            ],
        )

    return CheckResult(
        "substanz",
        0.75,
        [
            Finding(
                f"Startseite enthaelt fast keinen Text ({laenge} Zeichen)",
                "Die Startseite hat praktisch keinen Inhalt - weder fuer Besucher "
                "noch fuer Google gibt es dort etwas zu finden.",
            )
        ],
    )


def check_erreichbarkeit(ctx: Context) -> CheckResult:
    f = ctx.fetched
    signals: list[float] = []
    findings: list[Finding] = []

    if f.error:
        return CheckResult(
            "erreichbarkeit",
            1.0,
            [
                Finding(
                    f"Seite nicht erreichbar: {f.error}",
                    "Die Website war beim Test nicht erreichbar - das kostet direkt Anfragen.",
                )
            ],
        )

    if f.status is not None and f.status >= 500:
        signals.append(0.9)
        findings.append(
            Finding(
                f"Server antwortet mit HTTP {f.status}",
                "Der Server liefert einen Fehler statt der Seite aus.",
            )
        )
    elif f.status is not None and f.status >= 400:
        signals.append(0.8)
        findings.append(
            Finding(
                f"Startseite antwortet mit HTTP {f.status}",
                "Die Startseite liefert einen Fehlercode zurueck.",
            )
        )

    if f.redirects > 2:
        signals.append(0.35)
        findings.append(
            Finding(f"Weiterleitungskette ueber {f.redirects} Stationen")
        )

    if f.elapsed > 5.0:
        signals.append(0.4)
        findings.append(
            Finding(
                f"Startseite braucht {f.elapsed:.1f} s bis zur Antwort",
                f"Die Startseite laedt {f.elapsed:.1f} Sekunden - Besucher springen vorher ab.",
            )
        )

    return CheckResult("erreichbarkeit", combine(signals), findings)


def check_https(ctx: Context) -> CheckResult:
    f = ctx.fetched
    if f.error:
        return CheckResult("https", None)

    signals: list[float] = []
    findings: list[Finding] = []
    is_https = f.final_url.startswith("https://")

    if f.tls_problem:
        signals.append(1.0)
        reason = f.tls_problem
        if "expired" in reason.lower():
            text = "TLS-Zertifikat ist abgelaufen"
        else:
            text = f"TLS-Zertifikat ungueltig ({reason[:90]})"
        findings.append(
            Finding(
                text,
                "Das Sicherheitszertifikat ist ungueltig - Browser zeigen Besuchern eine Warnseite.",
            )
        )
    elif not is_https:
        signals.append(1.0)
        findings.append(
            Finding(
                "Seite laeuft nur ueber http, keine Verschluesselung",
                "Die Seite laeuft ohne Verschluesselung; Browser markieren sie als 'Nicht sicher'.",
            )
        )
    elif f.scheme_downgrade:
        signals.append(0.8)
        findings.append(Finding("https nicht erreichbar, nur http funktioniert"))

    if ctx.tree is not None and is_https:
        insecure = set()
        for node in ctx.tree.css("img[src], script[src], iframe[src], source[src], video[src], audio[src]"):
            src = (node.attributes.get("src") or "").strip()
            if src.lower().startswith("http://"):
                insecure.add(src)
        for node in ctx.tree.css("link[rel]"):
            attrs = node.attributes
            if "stylesheet" in (attrs.get("rel") or "").lower():
                href = (attrs.get("href") or "").strip()
                if href.lower().startswith("http://"):
                    insecure.add(href)
        if insecure:
            signals.append(0.6)
            findings.append(
                Finding(
                    f"Mixed Content: {len(insecure)} Ressource(n) werden ueber http nachgeladen",
                    "Trotz https werden Inhalte unverschluesselt nachgeladen - das Schloss-Symbol faellt weg.",
                )
            )

    return CheckResult("https", combine(signals), findings)


def check_mobil(ctx: Context) -> CheckResult:
    if ctx.tree is None:
        return CheckResult("mobil", None)

    viewport = meta_content(ctx.tree, "viewport")
    if not viewport:
        return CheckResult(
            "mobil",
            1.0,
            [
                Finding(
                    "Kein Viewport-Meta-Tag - Seite ist nicht fuer Mobilgeraete ausgelegt",
                    "Seite nicht mobiltauglich, es fehlt das Viewport-Tag - auf dem Handy erscheint alles winzig.",
                )
            ],
        )

    signals: list[float] = []
    findings: list[Finding] = []
    vp = viewport.lower().replace(" ", "")

    if "user-scalable=0" in vp or "user-scalable=no" in vp:
        signals.append(0.6)
        findings.append(
            Finding(
                "Zoom per user-scalable gesperrt",
                "Besucher koennen auf dem Handy nicht zoomen - ein Problem fuer aeltere Zielgruppen.",
            )
        )
    elif re.search(r"maximum-scale=1(\.0)?\b", vp):
        signals.append(0.3)
        findings.append(Finding("Zoom ueber maximum-scale=1 praktisch gesperrt"))

    if "width=device-width" not in vp:
        signals.append(0.5)
        findings.append(Finding("Viewport ohne width=device-width, feste Breite"))

    return CheckResult("mobil", combine(signals), findings)


FOOTER_YEAR_RE = re.compile(
    r"(?:©|&copy;|\(c\)|copyright)[^0-9]{0,25}((?:19|20)\d{2})"
    r"(?:\s*[–\-—/]\s*((?:19|20)\d{2}))?",
    re.IGNORECASE,
)


def footer_text(tree: LexborHTMLParser) -> str:
    parts: list[str] = []
    for node in tree.css("footer, [class*=footer], [id*=footer], [class*=Footer], [id*=Footer]"):
        parts.append(node.text(separator=" ", strip=True))
    if parts:
        return " ".join(parts)
    body = visible_text(tree)
    return body[-2500:]


def copyright_year(text: str) -> int | None:
    years: list[int] = []
    for m in FOOTER_YEAR_RE.finditer(text):
        for g in m.groups():
            if g:
                years.append(int(g))
    return max(years) if years else None


def check_aktualitaet(ctx: Context) -> CheckResult:
    if ctx.tree is None:
        return CheckResult("aktualitaet", None)

    signals: list[float] = []
    findings: list[Finding] = []

    year = copyright_year(footer_text(ctx.tree))
    if year is not None and year < STALE_YEAR:
        age = CURRENT_YEAR - year
        if age >= 10:
            sev = 1.0
        elif age >= 6:
            sev = 0.8
        else:
            sev = 0.5
        signals.append(sev)
        findings.append(
            Finding(
                f"Copyright im Footer steht auf {year}",
                f"Im Footer steht noch {year} - fuer Besucher sieht die Seite verlassen aus.",
            )
        )

    lm = ctx.fetched.headers.get("last-modified")
    if lm:
        try:
            dt = parsedate_to_datetime(lm)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - dt).days
            if days > 730:
                signals.append(0.6)
                findings.append(
                    Finding(f"Startseite laut Server seit {days // 365} Jahren unveraendert")
                )
            elif days > 365:
                signals.append(0.3)
                findings.append(Finding(f"Startseite seit {days} Tagen unveraendert"))
        except (TypeError, ValueError):
            pass

    return CheckResult("aktualitaet", combine(signals), findings)


# (Name, Severity, Argument-Satz) je Fingerabdruck
BAUKASTEN = [
    ("Jimdo", 0.85, "Die Seite laeuft auf dem Jimdo-Baukasten - Design und Funktionsumfang sind dort fest begrenzt."),
    ("Wix", 0.8, "Die Seite laeuft auf Wix - Inhalte lassen sich von dort nur schwer mitnehmen."),
    ("Webnode", 0.8, "Die Seite laeuft auf dem Webnode-Baukasten."),
    ("IONOS MyWebsite", 0.8, "Die Seite laeuft auf dem IONOS-Baukasten - technisch kaum erweiterbar."),
    ("Weebly", 0.8, "Die Seite laeuft auf dem Weebly-Baukasten."),
]


# Aktuelle Hauptversion des Plugins "Slider Revolution". Stand Dezember 2025:
# Plugin 6.7.40 (sliderrevolution.com/changelog). Die "SR7 Velocity Engine"
# steckt innerhalb der 6.7er-Reihe, die Plugin-Nummer blieb bei 6 - 6.x ist
# also die gepflegte Reihe, nicht etwa veraltet. Hebt Themepunch die
# Hauptnummer auf 7, hier anpassen.
SLIDERREV_AKTUELLE_HAUPT = 6

_SR_VERSION_IM_TEXT = re.compile(r"slider\s+revolution\s+v?(\d+(?:\.\d+)+)", re.IGNORECASE)
_SR_VERSION_IM_KOMMENTAR = re.compile(r"revolution\s+slider\s+v?(\d+(?:\.\d+)+)", re.IGNORECASE)
# Versionsparameter an Dateien des Plugins, z. B. rs6.min.js?ver=6.7.38. Nur
# Adressen, in denen das Plugin vorkommt - ein ?ver= an jquery.js ist die
# WordPress-Version, nicht die des Sliders.
_SR_VERSION_IM_ASSET = re.compile(
    r"(?:revslider|rs-plugin|revolution)[^\"'\s<>]*?[?&](?:ver|v|rev)=(\d+(?:\.\d+)+)", re.IGNORECASE
)
# Die Hauptnummer steckt oft schon im Dateipfad: .../revslider/sr6/... oder rs6.min.js
_SR_HAUPT_IM_PFAD = re.compile(r"revslider[^\"'\s<>]*?/(?:sr|rs)(\d)(?:/|\.)", re.IGNORECASE)


@dataclass(frozen=True)
class SliderRevVersion:
    text: str  # "6.7.38" oder "5.x oder aelter"
    haupt: int
    quelle: str  # Generator-Meta | Asset-Parameter | Kommentar | Dateipfad


def slider_revolution_version(tree: LexborHTMLParser, html: str) -> SliderRevVersion | None:
    """Version von Slider Revolution, soweit die Seite sie preisgibt.

    Nur ein Versionsnachweis taugt als Grundlage fuer ein Sicherheits-
    argument. Das blosse Vorhandensein des Plugins sagt nichts: die
    gepflegte 6.x-Reihe ist so unauffaellig wie jedes andere Plugin.
    Rueckgabe None, wenn keine Version ermittelbar ist.
    """

    def bauen(version: str, quelle: str) -> SliderRevVersion:
        return SliderRevVersion(version, int(version.split(".")[0]), quelle)

    # 1. Generator-Meta - Seiten haben oft mehrere, der Slider ist selten der erste
    for node in tree.css("meta"):
        if (node.attributes.get("name") or "").strip().lower() == "generator":
            m = _SR_VERSION_IM_TEXT.search(node.attributes.get("content") or "")
            if m:
                return bauen(m.group(1), "Generator-Meta")

    # 2. Versionsparameter an Plugin-Dateien
    m = _SR_VERSION_IM_ASSET.search(html)
    if m:
        return bauen(m.group(1), "Asset-Parameter")

    # 3. Kommentar im Quelltext, z. B. "START REVOLUTION SLIDER 5.4.8"
    m = _SR_VERSION_IM_KOMMENTAR.search(html)
    if m:
        return bauen(m.group(1), "Kommentar")

    # 4. Nur die Hauptnummer aus dem Dateipfad (sr6/, rs6.min.js)
    m = _SR_HAUPT_IM_PFAD.search(html)
    if m:
        return SliderRevVersion(f"{m.group(1)}.x", int(m.group(1)), "Dateipfad")
    # Der Ordner rs-plugin gehoert zu den Reihen 4 und 5
    if re.search(r"revslider/rs-plugin/", html, re.IGNORECASE):
        return SliderRevVersion("5.x oder aelter", 5, "Dateipfad")
    return None


def detect_stack(ctx: Context) -> tuple[list[tuple[str, float, str | None]], str]:
    """Erkannte Technik zurueckgeben plus kurzen CMS-Namen fuer die Tabelle."""
    if ctx.tree is None:
        return [], ""

    html = ctx.fetched.html
    low = html.lower()
    headers = {k.lower(): (v or "").lower() for k, v in ctx.fetched.headers.items()}
    generator = (meta_content(ctx.tree, "generator") or "").strip()
    gen_low = generator.lower()
    hits: list[tuple[str, float, str | None]] = []
    label = ""

    def has(*needles: str) -> bool:
        return any(n in low for n in needles)

    if has("jimdo", "cdn.jimdo.com") or "jimdo" in gen_low:
        hits.append((BAUKASTEN[0][0], BAUKASTEN[0][1], BAUKASTEN[0][2]))
        label = "Jimdo"
    if has("static.parastorage.com", "wix.com", "_wixcssimports") or "wix" in headers.get("x-wix-request-id", ""):
        hits.append((BAUKASTEN[1][0], BAUKASTEN[1][1], BAUKASTEN[1][2]))
        label = label or "Wix"
    if has("webnode.com", "webnode.cz", "webnode"):
        hits.append((BAUKASTEN[2][0], BAUKASTEN[2][1], BAUKASTEN[2][2]))
        label = label or "Webnode"
    if has("cdn.website-start.de", "mywebsite-editor", "ionos-mywebsite", "1and1.com/mywebsite"):
        hits.append((BAUKASTEN[3][0], BAUKASTEN[3][1], BAUKASTEN[3][2]))
        label = label or "IONOS MyWebsite"
    if has("weebly.com", "editmysite.com"):
        hits.append((BAUKASTEN[4][0], BAUKASTEN[4][1], BAUKASTEN[4][2]))
        label = label or "Weebly"

    # WordPress
    wp_version = None
    m = re.search(r"wordpress\s*([\d.]+)", gen_low)
    if m:
        wp_version = m.group(1)
    is_wp = bool(wp_version) or has("/wp-content/", "/wp-includes/", "wp-json")
    if is_wp:
        label = label or ("WordPress " + wp_version if wp_version else "WordPress")
        if wp_version:
            parts = wp_version.split(".")
            try:
                major = int(parts[0])
                minor = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                major, minor = 99, 0
            if major < 6:
                hits.append(
                    (
                        f"WordPress {wp_version} (veraltet)",
                        0.9,
                        f"Das CMS laeuft auf WordPress {wp_version} - diese Version bekommt keine Sicherheitsupdates mehr.",
                    )
                )
            elif major == 6 and minor < 4:
                hits.append((f"WordPress {wp_version} (aeltere Version)", 0.4, None))
            else:
                hits.append((f"WordPress {wp_version}", 0.1, None))
        else:
            hits.append(("WordPress (Version nicht ermittelbar)", 0.15, None))

    m = re.search(r"joomla!?\s*([\d.]+)?", gen_low)
    if m or has("/media/jui/", "/media/system/js/"):
        ver = m.group(1) if m and m.group(1) else None
        label = label or ("Joomla " + ver if ver else "Joomla")
        sev = 0.7 if ver and ver.split(".")[0] in ("1", "2", "3") else 0.3
        hits.append((f"Joomla {ver}" if ver else "Joomla", sev, None))

    if "typo3" in gen_low or has("/typo3temp/", "/typo3conf/"):
        label = label or "TYPO3"
        hits.append(("TYPO3", 0.3, None))

    if has("revslider", "rs-plugin", "slider revolution"):
        sr = slider_revolution_version(ctx.tree, html)
        if sr is None:
            # Reiner Fingerabdruck: bleibt als Befund stehen, begruendet aber
            # kein Sicherheitsargument - ohne Version waere das geraten.
            hits.append(("Slider Revolution (Version nicht ermittelbar)", 0.15, None))
        elif sr.haupt < SLIDERREV_AKTUELLE_HAUPT:
            hits.append(
                (
                    f"Slider Revolution {sr.text} (veraltet, aktuell ist die Reihe {SLIDERREV_AKTUELLE_HAUPT}.x)",
                    0.7,
                    f"Das Slider-Revolution-Plugin laeuft in Version {sr.text}, aktuell ist die Reihe "
                    f"{SLIDERREV_AKTUELLE_HAUPT}.x - aeltere Versionen haben bekannte Sicherheitsluecken, "
                    "das sollte geprueft werden.",
                )
            )
        else:
            hits.append((f"Slider Revolution {sr.text}", 0.1, None))

    # Technik aus der Flash-Aera
    if has(".swf", "application/x-shockwave-flash"):
        hits.append(
            (
                "Flash-Inhalte",
                1.0,
                "Die Seite bindet Flash ein - das zeigt seit 2021 kein Browser mehr an.",
            )
        )

    if ctx.tree.css_first("frameset") or ctx.tree.css_first("frame"):
        hits.append(
            (
                "Frameset-Layout",
                1.0,
                "Die Seite ist noch mit Frames gebaut - eine Technik aus den 90ern, die Google kaum noch indexiert.",
            )
        )

    if ctx.tree.css_first("font") or ctx.tree.css_first("center") or ctx.tree.css_first("marquee"):
        hits.append(("Veraltete HTML-Tags (font/center/marquee)", 0.7, None))

    depth = 0
    for t in ctx.tree.css("table"):
        d = 1
        p = t.parent
        steps = 0
        while p is not None and steps < 50:
            if p.tag == "table":
                d += 1
            p = p.parent
            steps += 1
        depth = max(depth, d)
    if depth >= 3:
        hits.append(
            (
                f"Tabellenlayout ({depth} Ebenen verschachtelt)",
                0.9,
                "Das Layout ist mit verschachtelten Tabellen gebaut - damit laesst sich die Seite nicht mobil anpassen.",
            )
        )
    elif depth == 2:
        hits.append(("Verschachtelte Tabellen im Layout", 0.4, None))

    if not label and generator:
        label = generator[:40]
    return hits, label


def check_technologie(ctx: Context) -> CheckResult:
    if ctx.tree is None:
        return CheckResult("technologie", None)
    hits, _ = detect_stack(ctx)
    # Der Slider-Fingerabdruck steht immer als Befund da, auch wenn er kaum
    # ins Gewicht faellt - er soll sichtbar bleiben, ohne zu alarmieren.
    findings = [
        Finding(name, arg)
        for name, _sev, arg in hits
        if _sev >= 0.3 or arg or name.startswith("Slider Revolution")
    ]
    return CheckResult("technologie", combine([s for _n, s, _a in hits]), findings)


# Nur diese Codes gelten als toter Link. 429/5xx sind temporaere Antworten -
# oft ausgeloest durch unsere eigenen Requests kurz davor - und wuerden sonst
# als "Datenschutzerklaerung fehlt" im Verkaufsgespraech landen.
DEAD_LINK_CODES = frozenset({403, 404, 410})

LEGAL_WORDS_IMPRESSUM = ("impressum", "imprint", "anbieterkennzeichnung")
LEGAL_WORDS_DATENSCHUTZ = ("datenschutz", "privacy", "dsgvo")
# Eindeutige Spuren eines Consent-Tools. Bewusst nur harte Treffer - ein
# blosses "DSGVO" oder "Einwilligung" im Fliesstext steht oft in der
# Datenschutzerklaerung und beweist noch kein Banner.
CONSENT_MARKERS = (
    "cookiebot", "borlabs", "usercentrics", "klaro", "cookieconsent",
    "complianz", "cookie-notice", "cookiefirst", "consentmanager",
    "cookie-banner", "cookiebanner", "cmplz", "didomi", "osano", "iubenda",
    "cookie-hinweis", "cookie_consent", "cookie-consent", "cookienotice",
    "gdpr-cookie", "consent-manager", "__tcfapi", "cookieyes", "termly",
)
# Schwache Spur: "cookie" plus ein Zustimmungswort in Reichweite.
CONSENT_SOFT = re.compile(
    r"cookie[^<]{0,400}?(akzeptieren|zustimmen|einverstanden|alle erlauben|"
    r"auswahl bestaetigen|auswahl best\w*tigen|accept all|zustimmung)",
    re.IGNORECASE,
)


def has_consent_tool(tree: LexborHTMLParser, low: str) -> bool:
    """Erkennen, ob ueberhaupt ein Consent-Tool eingebunden ist.

    Eine Liste bekannter Produktnamen reicht nicht - es gibt zu viele Plugins
    und Eigenbauten. Zuverlaessiger ist die Spur, die jedes dieser Tools
    hinterlaesst: eigene CSS/JS-Dateien und einen Banner-Container, deren
    Namen "cookie" oder "consent" enthalten. Blosse Links werden bewusst
    ignoriert, sonst gilt schon ein Footer-Link "Cookie-Richtlinie" als Banner.
    """
    if any(m in low for m in CONSENT_MARKERS):
        return True

    for node in tree.css("script[src], link[href]"):
        attrs = node.attributes
        val = (attrs.get("src") or attrs.get("href") or "").lower()
        if "cookie" in val or "consent" in val:
            return True

    for node in tree.css("div[id], div[class], section[id], section[class], aside[id], aside[class]"):
        attrs = node.attributes
        ident = ((attrs.get("id") or "") + " " + (attrs.get("class") or "")).lower()
        if "cookie" in ident or "consent" in ident:
            return True

    # Inline-CSS eines Banners zaehlt auch - viele Themes liefern den
    # Container per JS nach, das Stylesheet steht aber schon im Quelltext.
    for node in tree.css("style"):
        css = node.text().lower()
        if re.search(r"[#.][\w-]*(cookie|consent)[\w-]*\s*[{,.:]", css):
            return True

    return bool(CONSENT_SOFT.search(low))


def find_legal_links(tree: LexborHTMLParser, base: str) -> tuple[str | None, str | None]:
    impressum = datenschutz = None
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        hay = (href + " " + a.text(strip=True) + " " + (a.attributes.get("title") or "")).lower()
        if impressum is None and any(w in hay for w in LEGAL_WORDS_IMPRESSUM):
            impressum = urljoin(base, href)
        if datenschutz is None and any(w in hay for w in LEGAL_WORDS_DATENSCHUTZ):
            datenschutz = urljoin(base, href)
        if impressum and datenschutz:
            break
    return impressum, datenschutz


def check_recht(ctx: Context) -> CheckResult:
    if ctx.tree is None:
        return CheckResult("recht", None)

    signals: list[float] = []
    findings: list[Finding] = []
    low = ctx.fetched.html.lower()
    legal = ctx.legal or LegalInfo()

    if not legal.impressum_url:
        signals.append(1.0)
        findings.append(
            Finding(
                "Kein Impressum-Link auf der Startseite gefunden - sollte geprueft werden",
                "Auf der Startseite ist kein Impressum verlinkt; ob das so passt, sollte geprueft werden.",
            )
        )
    elif legal.impressum_status in DEAD_LINK_CODES:
        signals.append(0.7)
        findings.append(
            Finding(f"Impressum-Link laeuft ins Leere (HTTP {legal.impressum_status})")
        )

    if not legal.datenschutz_url:
        signals.append(0.9)
        findings.append(
            Finding(
                "Kein Datenschutz-Link auf der Startseite gefunden - sollte geprueft werden",
                "Eine Datenschutzerklaerung ist nicht verlinkt; das sollte geprueft werden.",
            )
        )
    elif legal.datenschutz_status in DEAD_LINK_CODES:
        signals.append(0.6)
        findings.append(
            Finding(f"Datenschutz-Link laeuft ins Leere (HTTP {legal.datenschutz_status})")
        )

    uses_gfonts = "fonts.googleapis.com" in low or "fonts.gstatic.com" in low
    uses_analytics = any(
        m in low
        for m in ("google-analytics.com", "googletagmanager.com", "gtag(", "ga.js", "matomo", "piwik")
    )
    uses_maps = "maps.google" in low or "maps.googleapis.com" in low or "google.com/maps/embed" in low
    has_consent = has_consent_tool(ctx.tree, low)

    if uses_gfonts:
        signals.append(0.8)
        findings.append(
            Finding(
                "Google Fonts werden direkt von Google geladen - datenschutzrechtlich ein Risiko, sollte geprueft werden",
                "Google Fonts werden direkt von Google nachgeladen; ob dafuer eine Einwilligung vorliegt, sollte geprueft werden.",
            )
        )

    if (uses_analytics or uses_maps or uses_gfonts) and not has_consent:
        dienste = ", ".join(
            n for n, on in (("Google Fonts", uses_gfonts), ("Analytics", uses_analytics), ("Google Maps", uses_maps)) if on
        )
        signals.append(0.7)
        findings.append(
            Finding(
                f"Externe Dienste eingebunden ({dienste}), aber kein Cookie-/Consent-Banner erkennbar - sollte geprueft werden",
                f"Es sind externe Dienste eingebunden ({dienste}), ohne dass ein Einwilligungsbanner erkennbar ist - das sollte geprueft werden.",
            )
        )

    return CheckResult("recht", combine(signals), findings)


def check_seo(ctx: Context) -> CheckResult:
    if ctx.tree is None:
        return CheckResult("seo", None)

    signals: list[float] = []
    findings: list[Finding] = []
    tree = ctx.tree

    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    if not title:
        signals.append(1.0)
        findings.append(
            Finding(
                "Kein Seitentitel gesetzt",
                "Die Seite hat keinen Titel - bei Google steht dort nur die Adresse.",
            )
        )
    else:
        if len(title) > TITLE_MAX:
            signals.append(0.3)
            findings.append(Finding(f"Seitentitel zu lang ({len(title)} Zeichen)"))
        elif len(title) < 10:
            signals.append(0.5)
            findings.append(Finding(f"Seitentitel sehr kurz ({len(title)} Zeichen)"))
        words = [w for w in re.findall(r"[\wäöüßÄÖÜ]{4,}", title.lower())]
        repeated = {w for w in words if words.count(w) >= 3}
        separators = len(re.findall(r"[|,/\-–]", title))
        if repeated or separators > 6:
            signals.append(0.4)
            findings.append(
                Finding(
                    "Seitentitel wirkt mit Suchbegriffen ueberladen",
                    "Der Seitentitel ist mit Suchbegriffen ueberladen - das wertet Google eher ab.",
                )
            )

    desc = meta_content(tree, "description")
    if not desc:
        signals.append(0.6)
        findings.append(
            Finding(
                "Keine Meta-Description",
                "Es fehlt die Beschreibung fuer die Google-Trefferliste - dort steht dann zufaelliger Text.",
            )
        )
    elif len(desc) > DESC_MAX:
        signals.append(0.15)
        findings.append(Finding(f"Meta-Description zu lang ({len(desc)} Zeichen)"))

    h1 = tree.css("h1")
    if len(h1) == 0:
        signals.append(0.6)
        findings.append(Finding("Keine H1-Ueberschrift auf der Startseite"))
    elif len(h1) > 1:
        signals.append(0.3)
        findings.append(Finding(f"{len(h1)} H1-Ueberschriften statt einer"))

    imgs = tree.css("img")
    if imgs:
        missing = sum(1 for i in imgs if not (i.attributes.get("alt") or "").strip())
        ratio = missing / len(imgs)
        if ratio > 0.3:
            signals.append(min(1.0, ratio))
            findings.append(
                Finding(
                    f"{missing} von {len(imgs)} Bildern ohne alt-Text ({ratio:.0%})",
                    f"{ratio:.0%} der Bilder haben keinen Alternativtext - schlecht fuer Google und fuer Screenreader.",
                )
            )

    return CheckResult("seo", combine(signals), findings)


def check_bilder(ctx: Context) -> CheckResult:
    if ctx.tree is None or ctx.assets is None:
        return CheckResult("bilder", None)

    a = ctx.assets
    signals: list[float] = []
    findings: list[Finding] = []
    total = a.total_bytes + ctx.fetched.html_bytes

    if a.measured:
        if total > 5_000_000:
            signals.append(1.0)
        elif total > 3_000_000:
            signals.append(0.6)
        elif total > 2_000_000:
            signals.append(0.3)
        if total > 2_000_000:
            hint = " (Stichprobe)" if a.measured < a.refs else ""
            findings.append(
                Finding(
                    f"Seitengewicht ca. {human_bytes(total)}{hint}",
                    f"Die Startseite laedt rund {human_bytes(total)} - ueber Mobilfunk dauert das spuerbar.",
                )
            )

    if a.oversized:
        signals.append(0.8)
        worst = max(a.oversized, key=lambda x: x[1])
        findings.append(
            Finding(
                f"{len(a.oversized)} unkomprimierte(s) Bild(er) ueber 1,5 MB, groesstes {human_bytes(worst[1])}",
                f"Einzelne Bilder sind bis zu {human_bytes(worst[1])} gross und ungekomprimiert eingebunden.",
            )
        )

    if a.images_measured >= MIN_INHALTSBILDER:
        ratio = a.images_small / a.images_measured
        if ratio > 0.5:
            signals.append(0.5)
            findings.append(
                Finding(
                    f"{ratio:.0%} der gemessenen Bilder sind schmaler als {SMALL_IMAGE_PX} px",
                    "Die Bilder sind fuer heutige Bildschirme zu klein und wirken unscharf.",
                )
            )

    return CheckResult("bilder", combine(signals), findings)


def check_kontakt(ctx: Context) -> CheckResult:
    if ctx.tree is None:
        return CheckResult("kontakt", None)

    tree = ctx.tree
    has_tel = bool(tree.css("a[href^='tel:'], a[href^='TEL:']"))
    if not has_tel:
        for a in tree.css("a[href]"):
            if (a.attributes.get("href") or "").lower().startswith("tel:"):
                has_tel = True
                break

    has_form = False
    for form in tree.css("form"):
        if form.css("input, textarea"):
            hay = (form.html or "").lower()
            if not any(w in hay for w in ("search", "suche", "newsletter", "login", "anmeld")):
                has_form = True
                break
    if not has_form:
        for a in tree.css("a[href]"):
            href = (a.attributes.get("href") or "").lower()
            if "kontakt" in href or "contact" in href or "anfrage" in href:
                has_form = True
                break

    signals: list[float] = []
    findings: list[Finding] = []
    if not has_tel and not has_form:
        signals.append(1.0)
        findings.append(
            Finding(
                "Weder klickbare Telefonnummer noch Kontaktformular/-seite",
                "Auf dem Handy gibt es keine Moeglichkeit, direkt anzurufen oder eine Anfrage zu schicken.",
            )
        )
    elif not has_tel:
        signals.append(0.5)
        findings.append(
            Finding(
                "Keine klickbare Telefonnummer (tel:-Link)",
                "Die Telefonnummer ist auf dem Handy nicht antippbar - jeder Anruf wird zur Tipparbeit.",
            )
        )
    elif not has_form:
        signals.append(0.35)
        findings.append(Finding("Kein Kontaktformular und keine Kontaktseite verlinkt"))

    return CheckResult("kontakt", combine(signals), findings)


def check_konsistenz(ctx: Context) -> CheckResult:
    if ctx.tree is None:
        return CheckResult("konsistenz", None)

    desc = meta_content(ctx.tree, "description") or ""
    if not desc:
        return CheckResult("konsistenz", 0.0)

    desc_phones = find_phones(desc)
    if not desc_phones:
        return CheckResult("konsistenz", 0.0)

    # Nur im sichtbaren Text und in tel:-Links suchen. Das Roh-HTML wuerde die
    # Meta-Description selbst enthalten - dann faende der Check nie eine
    # Abweichung, weil die gesuchte Nummer immer auch "im Text" steht.
    tel_links = " ".join(
        a.attributes.get("href") or ""
        for a in ctx.tree.css("a[href]")
        if (a.attributes.get("href") or "").lower().startswith("tel:")
    )
    body_phones = find_phones(visible_text(ctx.tree) + " " + tel_links)
    orphan = desc_phones - body_phones
    if orphan and body_phones:
        return CheckResult(
            "konsistenz",
            1.0,
            [
                Finding(
                    "Telefonnummer in der Meta-Description weicht von der Nummer auf der Seite ab",
                    "In den Seiteninformationen steht eine andere Telefonnummer als im Seitentext.",
                )
            ],
        )
    return CheckResult("konsistenz", 0.0)


def check_pagespeed(ctx: Context) -> CheckResult:
    if not ctx.psi_ran or ctx.psi_score is None:
        return CheckResult("pagespeed", None)
    score = ctx.psi_score
    sev = max(0.0, (100 - score) / 100)
    findings: list[Finding] = []
    if score < 50:
        findings.append(
            Finding(
                f"PageSpeed mobil: {score}/100",
                f"Google bewertet die mobile Geschwindigkeit mit {score} von 100 Punkten.",
            )
        )
    elif score < 80:
        findings.append(Finding(f"PageSpeed mobil: {score}/100"))
    return CheckResult("pagespeed", round(sev, 4), findings)


CHECKS = [
    check_substanz,
    check_erreichbarkeit,
    check_https,
    check_mobil,
    check_aktualitaet,
    check_technologie,
    check_recht,
    check_seo,
    check_bilder,
    check_kontakt,
    check_konsistenz,
    check_pagespeed,
]

LABELS = {
    "substanz": "Inhalt",
    "erreichbarkeit": "Erreichbarkeit",
    "https": "HTTPS",
    "mobil": "Mobil",
    "aktualitaet": "Aktualitaet",
    "technologie": "Technik",
    "recht": "Rechtliches",
    "seo": "SEO",
    "bilder": "Bilder",
    "kontakt": "Kontakt",
    "konsistenz": "Konsistenz",
    "pagespeed": "PageSpeed",
}


def score_from_checks(results: list[CheckResult], weights: dict[str, float] | None = None) -> float:
    """Chaos-Score 0-100, hoeher = schlechter.

    Bezugsgroesse ist SCORE_REFERENZ, nicht die volle Gewichtssumme. Checks
    ohne Ergebnis (uebersprungen oder nicht anwendbar) fallen aus der Rechnung
    heraus, und die Referenz schrumpft im gleichen Verhaeltnis mit. Dadurch
    verschiebt `--pagespeed` die Skala nicht.
    """
    w = weights or WEIGHTS
    num = gelaufen = 0.0
    for r in results:
        if r.severity is None:
            continue
        weight = w.get(r.key, 0.0)
        num += weight * r.severity
        gelaufen += weight
    gesamt = sum(w.values())
    if gelaufen == 0 or gesamt == 0:
        return 0.0
    referenz = SCORE_REFERENZ * gelaufen / gesamt
    if referenz <= 0:
        return 0.0
    return round(min(100.0, num / referenz * 100), 1)


def build_arguments(results: list[CheckResult], weights: dict[str, float] | None = None, limit: int = 4) -> list[str]:
    """2-4 kurze Saetze fuers Erstgespraech, nach Gewicht x Severity sortiert."""
    w = weights or WEIGHTS
    scored: list[tuple[float, str]] = []
    for r in results:
        if r.severity is None or r.severity <= 0:
            continue
        rank = w.get(r.key, 0.0) * r.severity
        for f in r.findings:
            if f.argument:
                scored.append((rank, f.argument))
    scored.sort(key=lambda x: -x[0])
    out: list[str] = []
    for _rank, text in scored:
        if text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Netz
# ---------------------------------------------------------------------------


class HostLimiter:
    """Haelt pro Host mindestens `interval` Sekunden Abstand zwischen Requests."""

    def __init__(self, interval: float = HOST_INTERVAL) -> None:
        self.interval = interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last.get(host)
            if last is not None:
                delta = time.monotonic() - last
                if delta < self.interval:
                    await asyncio.sleep(self.interval - delta)
            self._last[host] = time.monotonic()


class Fetcher:
    def __init__(self, timeout: float, limiter: HostLimiter, parallel: int = MAX_PARALLEL) -> None:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9",
        }
        # Das Verbindungslimit muss mit der Parallelitaet mitwachsen. Ist der Pool
        # zu klein, laufen Abrufe in einen Pool-Timeout - der als "nicht erreichbar"
        # gemeldet wuerde, obwohl die Seite in Ordnung ist.
        limits = httpx.Limits(max_connections=parallel * 3 + 10, max_keepalive_connections=parallel)
        zeitlimit = httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT), pool=max(timeout, 60.0))
        self.secure = httpx.AsyncClient(
            headers=headers, timeout=zeitlimit, follow_redirects=True, limits=limits, verify=True
        )
        self.insecure = httpx.AsyncClient(
            headers=headers, timeout=zeitlimit, follow_redirects=True, limits=limits, verify=False
        )
        self.limiter = limiter
        # Hosts, die es nicht gibt. Wer beim robots.txt-Abruf scheitert, spart sich
        # beim Seitenabruf die Wiederholungen, den http-Versuch und die Wartezeit.
        self._tot: dict[str, str] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self.secure.aclose()
        await self.insecure.aclose()

    async def robots_allows(self, url: str) -> bool:
        parsed = urlparse(url)
        key = f"{parsed.scheme}://{parsed.netloc}"
        lock = self._robots_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key not in self._robots:
                self._robots[key] = await self._load_robots(key, parsed.hostname or "")
        rp = self._robots[key]
        if rp is None:
            return True
        try:
            return rp.can_fetch(ROBOTS_AGENT, url)
        except Exception:
            return True

    async def _load_robots(self, base: str, host: str) -> RobotFileParser | None:
        try:
            await self.limiter.wait(host)
            resp = await self.secure.get(base + "/robots.txt")
        except Exception as exc:  # noqa: BLE001
            fehler = describe_error(exc)
            if ist_dauerhaft_tot(fehler):
                self._tot[host] = fehler
                return None
            # Der unsichere Zweitversuch hilft nur bei einem Zertifikatsproblem. Bei
            # Timeout oder Verbindungsfehler verdoppelte er nur die Wartezeit.
            if not ssl_reason(exc):
                return None
            try:
                await self.limiter.wait(host)
                resp = await self.insecure.get(base + "/robots.txt")
            except Exception:  # noqa: BLE001
                return None
        # 4xx/5xx werten wir als "keine Regeln" - sonst fallen Leads
        # wegen einer kaputten robots.txt grundlos aus der Liste.
        if resp.status_code >= 400:
            return None
        rp = RobotFileParser()
        try:
            rp.parse(resp.text.splitlines())
        except Exception:
            return None
        return rp

    async def get_page(self, candidates: list[str]) -> Fetched:
        last_error = ""
        for idx, url in enumerate(candidates):
            host = host_of(url)
            if host in self._tot:  # beim robots.txt-Abruf schon als nicht existent erkannt
                last_error = self._tot[host]
                continue
            tls_problem = None
            resp = None
            host_tot = False
            started = time.monotonic()
            # Erst mehrfach versuchen, bevor wir eine Seite aufgeben oder auf
            # http zurueckfallen. Ein einzelner Netzaussetzer darf eine Seite
            # weder als "nicht erreichbar" noch als "kein https" abstempeln.
            for attempt in range(RETRIES):
                try:
                    await self.limiter.wait(host)
                    resp = await self.secure.get(url)
                    break
                except Exception as exc:  # noqa: BLE001 - pro Seite abfangen, nie abbrechen
                    reason = ssl_reason(exc)
                    if reason:
                        # Zertifikatsfehler sind stabil, Wiederholen bringt nichts.
                        tls_problem = reason
                        try:
                            await self.limiter.wait(host)
                            resp = await self.insecure.get(url)
                        except Exception as exc2:  # noqa: BLE001
                            last_error = describe_error(exc2)
                            resp = None
                        break
                    last_error = describe_error(exc)
                    resp = None
                    if ist_dauerhaft_tot(last_error):
                        # Die Adresse gibt es nicht: weder Wiederholen noch http helfen.
                        self._tot[host] = last_error
                        host_tot = True
                        break
                    if isinstance(exc, httpx.TimeoutException) and attempt + 1 >= TIMEOUT_VERSUCHE:
                        break
                    if attempt < RETRIES - 1:
                        await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
            if resp is None:
                if host_tot:
                    break  # derselbe Host unter http:// ist genauso tot
                continue

            elapsed = time.monotonic() - started
            body = resp.text

            # Meta-Refresh bis zu zweimal folgen
            gesehen = {str(resp.url)}
            weiter = refresh_ziel(body, str(resp.url))
            for _ in range(2):
                if not weiter or weiter in gesehen:
                    break
                gesehen.add(weiter)
                try:
                    await self.limiter.wait(host_of(weiter))
                    folge = await self.secure.get(weiter)
                except Exception:  # noqa: BLE001 - dann bleibt die Huelle
                    break
                if folge.status_code >= 400:
                    break
                resp = folge
                body = folge.text
                weiter = refresh_ziel(body, str(folge.url))

            return Fetched(
                url=candidates[0],
                final_url=str(resp.url),
                status=resp.status_code,
                headers={k.lower(): v for k, v in resp.headers.items()},
                html=body,
                html_bytes=len(resp.content),
                redirects=len(resp.history),
                elapsed=elapsed,
                tls_problem=tls_problem,
                scheme_downgrade=idx > 0 and candidates[0].startswith("https://"),
            )
        return Fetched(url=candidates[0] if candidates else "", error=last_error or "unbekannter Fehler")

    async def head_status(self, url: str) -> int | None:
        host = host_of(url)
        for method in ("HEAD", "GET"):
            try:
                await self.limiter.wait(host)
                resp = await self.secure.request(method, url)
                if resp.status_code in (405, 501) and method == "HEAD":
                    continue
                return resp.status_code
            except Exception:  # noqa: BLE001
                return None
        return None

    async def status_und_text(self, url: str) -> tuple[int | None, str]:
        """Statuscode und HTML einer Unterseite in einem Abruf.

        Ersetzt head_status() gefolgt von text_of() - zwei Abrufe an denselben
        Host, also zwei Sekunden Takt statt einer.
        """
        try:
            await self.limiter.wait(host_of(url))
            resp = await self.secure.get(url)
            return resp.status_code, (resp.text if resp.status_code < 400 else "")
        except Exception:  # noqa: BLE001 - eine Unterseite darf nie den Lauf kippen
            return None, ""

    async def text_of(self, url: str) -> str:
        """HTML einer Unterseite holen. Leerer String, wenn es nicht klappt."""
        try:
            await self.limiter.wait(host_of(url))
            resp = await self.secure.get(url)
            return resp.text if resp.status_code < 400 else ""
        except Exception:  # noqa: BLE001 - eine Unterseite darf nie den Lauf kippen
            return ""

    async def measure_asset(self, url: str) -> tuple[int, bytes] | None:
        """Groesse und die ersten Bytes eines Assets holen (ein Request)."""
        host = host_of(url)
        try:
            await self.limiter.wait(host)
            async with self.secure.stream(
                "GET", url, headers={"Range": f"bytes=0-{ASSET_PEEK - 1}"}
            ) as resp:
                if resp.status_code >= 400:
                    return None
                chunks: list[bytes] = []
                got = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    got += len(chunk)
                    if got >= ASSET_PEEK:
                        break
                head = b"".join(chunks)
                total = got
                cr = resp.headers.get("content-range")
                if cr and "/" in cr:
                    tail = cr.rsplit("/", 1)[-1].strip()
                    if tail.isdigit():
                        total = int(tail)
                elif resp.status_code == 200:
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit():
                        total = int(cl)
                return total, head
        except Exception:  # noqa: BLE001
            return None


META_REFRESH = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*["']\s*(\d+)\s*;\s*"""
    r"""url\s*=\s*["']?([^"'>\s]+)""",
    re.IGNORECASE,
)

SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "tiktok.com", "youtube.com", "pinterest.de", "pinterest.com",
)


def refresh_ziel(html: str, basis: str) -> str | None:
    """Ziel einer <meta http-equiv="refresh">-Weiterleitung.

    httpx folgt nur HTTP-Weiterleitungen. Ohne das hier pruefen wir bei
    aelteren Seiten eine leere Huelle statt der echten Seite.
    """
    m = META_REFRESH.search(html[:4000])
    if not m:
        return None
    if int(m.group(1)) > 5:  # echte Verzoegerung, kein Umleitungstrick
        return None
    return urljoin(basis, m.group(2).strip())


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return "Timeout beim Verbindungsaufbau"
    if isinstance(exc, httpx.ReadTimeout):
        return "Timeout beim Laden"
    if isinstance(exc, httpx.TimeoutException):
        return "Timeout"
    if isinstance(exc, httpx.ConnectError):
        return f"Verbindung fehlgeschlagen ({str(exc)[:70]})"
    if isinstance(exc, httpx.TooManyRedirects):
        return "zu viele Weiterleitungen"
    return f"{type(exc).__name__}: {str(exc)[:70]}"


# Bilder, die nichts ueber die Bildqualitaet der Seite sagen: Platzhalter, die
# ein Lazy-Load-Skript spaeter durch das echte Bild ersetzt.
PLATZHALTER_MARKER = (
    "a3-lazy-load", "lazy_placeholder", "lazy-placeholder", "placeholder",
    "dummy.png", "dummy.gif", "blank.gif", "blank.png", "spacer", "transparent.",
    "1x1", "pixel.gif", "loading.gif", "loader.gif", "grey.gif", "lazy.gif",
)

# Kein Inhaltsbild, wenn Klasse, Id, Alt-Text, Titel oder Dateiname eines davon enthaelt.
BILD_KEIN_INHALT = (
    "logo", "icon", "favicon", "sprite", "pixel", "tracking", "counter", "beacon",
    "spacer", "emoji", "smiley", "avatar", "badge", "rating", "bewertung", "siegel",
    "payment", "captcha", "arrow", "bullet", "button",
)

# Bereiche, in denen keine Inhaltsbilder stehen: Kopf, Navigation, Fuss, Seitenleiste.
KOPF_FUSS_TAGS = frozenset({"header", "nav", "footer", "aside", "noscript"})
KOPF_FUSS_ROLLEN = frozenset({"banner", "navigation", "contentinfo", "complementary"})
KOPF_FUSS_WORTE = ("masthead", "navbar", "site-header", "site-footer", "main-nav", "topbar", "breadcrumb")

ICON_MAX_KANTE = 64  # bis hierhin gilt ein Bild als Icon
PIXEL_MAX_KANTE = 3  # darunter ist es ein Zaehl- oder Platzhalterpixel
MIN_INHALTSBILDER = 3  # erst ab so vielen Inhaltsbildern ist eine Anteilsaussage belastbar


@dataclass
class BildRef:
    url: str
    inhalt: bool  # Inhaltsbild, nicht Logo/Icon/Zaehlpixel
    breite: int | None = None  # aus dem srcset-Deskriptor, falls vorhanden


def ist_platzhalter(url: str) -> bool:
    """Ersatzbild eines Lazy-Load-Skripts (data:-URI, 1x1-GIF, Dummy-Datei)."""
    if not url:
        return True
    klein = url.strip().lower()
    if klein.startswith(("data:", "about:", "javascript:")):
        return True
    return any(marker in klein for marker in PLATZHALTER_MARKER)


def srcset_kandidaten(srcset: str) -> list[tuple[str, int | None, float]]:
    """srcset zerlegen: (Adresse, Breite aus "600w" oder None, Dichte aus "2x")."""
    ergebnis: list[tuple[str, int | None, float]] = []
    for teil in re.split(r"\s*,\s+", (srcset or "").strip()):
        stuecke = teil.split()
        if not stuecke:
            continue
        # Uebrig gebliebene Kommas ("a.jpg 1x,") gehoeren nicht zur Adresse; ein
        # Eintrag ohne Zeichen davon ist Muell und kein Bild.
        stuecke[0] = stuecke[0].strip(",")
        if not stuecke[0]:
            continue
        breite: int | None = None
        dichte = 1.0
        if len(stuecke) > 1:
            deskriptor = stuecke[1].lower()
            if deskriptor.endswith("w") and deskriptor[:-1].isdigit():
                breite = int(deskriptor[:-1])
            elif deskriptor.endswith("x"):
                try:
                    dichte = float(deskriptor[:-1])
                except ValueError:
                    pass
        ergebnis.append((stuecke[0], breite, dichte))
    return ergebnis


def bildquelle(node) -> tuple[str, int | None]:
    """Adresse des echten Bildes und - falls bekannt - seine Breite.

    Reihenfolge: das groesste Element aus (data-)srcset, dann data-src,
    data-lazy-src, data-original, data-lazyload, zuletzt src. Platzhalter
    werden uebersprungen; bleibt nur einer uebrig, gibt es kein Bild.
    """
    a = node.attributes
    kandidaten: list[tuple[str, int | None, float]] = []
    for schluessel in ("data-srcset", "data-lazy-srcset", "srcset"):
        kandidaten += srcset_kandidaten(a.get(schluessel) or "")
    brauchbar = [k for k in kandidaten if not ist_platzhalter(k[0])]
    if brauchbar:
        url, breite, _dichte = max(brauchbar, key=lambda k: (k[1] or 0, k[2]))
        return url, breite
    for schluessel in ("data-src", "data-lazy-src", "data-original", "data-lazyload", "src"):
        wert = (a.get(schluessel) or "").strip()
        if wert and not ist_platzhalter(wert):
            return wert, None
    return "", None


def _kante(wert: str | None) -> int | None:
    m = re.match(r"^\s*(\d+)\s*(?:px)?\s*$", wert or "")
    return int(m.group(1)) if m else None


def ist_inhaltsbild(node, url: str) -> bool:
    """Foto oder Grafik im sichtbaren Inhalt - kein Logo, Icon oder Zaehlpixel."""
    pfad = url.lower().split("?")[0]
    if pfad.endswith(".svg"):
        return False  # Vektorgrafik, die Pixelbreite sagt nichts
    a = node.attributes
    eigene = " ".join(
        [a.get("class") or "", a.get("id") or "", a.get("alt") or "", a.get("title") or "", pfad.rsplit("/", 1)[-1]]
    ).lower()
    if any(wort in eigene for wort in BILD_KEIN_INHALT):
        return False

    kanten = [k for k in (_kante(a.get("width")), _kante(a.get("height"))) if k]
    if kanten and max(kanten) <= ICON_MAX_KANTE:
        return False

    oben = node.parent
    for _ in range(15):
        if oben is None:
            break
        if oben.tag in KOPF_FUSS_TAGS:
            return False
        if (oben.attributes.get("role") or "").lower() in KOPF_FUSS_ROLLEN:
            return False
        kennung = ((oben.attributes.get("class") or "") + " " + (oben.attributes.get("id") or "")).lower()
        if any(wort in kennung for wort in KOPF_FUSS_WORTE):
            return False
        oben = oben.parent
    return True


def bild_refs(tree: LexborHTMLParser, base: str) -> list[BildRef]:
    """Alle echten Bilder der Seite, Platzhalter bereits aussortiert."""
    refs: dict[str, BildRef] = {}
    for node in tree.css("img"):
        roh, breite = bildquelle(node)
        if not roh:
            continue
        voll = urljoin(base, roh.strip())
        if not voll.lower().startswith(("http://", "https://")):
            continue
        inhalt = ist_inhaltsbild(node, voll)
        if voll in refs:
            # Dasselbe Bild an anderer Stelle: zaehlt, sobald es irgendwo Inhalt ist
            refs[voll].inhalt = refs[voll].inhalt or inhalt
            refs[voll].breite = refs[voll].breite or breite
            continue
        refs[voll] = BildRef(voll, inhalt, breite)
    return list(refs.values())


def zaehle_inhaltsbilder(
    refs: list[BildRef], masse: dict[str, tuple[int, int] | None]
) -> tuple[int, int]:
    """(bewertete Inhaltsbilder, davon schmaler als SMALL_IMAGE_PX).

    Die Breite kommt aus dem srcset-Deskriptor, sonst aus dem Dateikopf des
    vermessenen Bildes. Bilder ohne bekannte Breite und Zaehlpixel zaehlen
    nicht - lieber weniger Bilder bewerten als falsche.
    """
    gemessen = klein = 0
    for ref in refs:
        if not ref.inhalt:
            continue
        dims = masse.get(ref.url)
        if dims and max(dims) <= PIXEL_MAX_KANTE:
            continue
        breite = ref.breite or (dims[0] if dims else None)
        if not breite:
            continue
        gemessen += 1
        if breite < SMALL_IMAGE_PX:
            klein += 1
    return gemessen, klein


def collect_asset_urls(tree: LexborHTMLParser, base: str, limit: int) -> tuple[list[str], list[str], int]:
    """Bilder zuerst (Inhaltsbilder vor Logos), danach CSS/JS.

    Rueckgabe: (bilder, sonstige, gesamt_refs). Platzhalter und data:-URIs
    sind nicht dabei.
    """
    refs = bild_refs(tree, base)
    refs.sort(key=lambda r: not r.inhalt)  # stabil: Inhaltsbilder zuerst
    images = [r.url for r in refs]
    others: list[str] = []
    seen: set[str] = set()

    def add(target: list[str], raw: str) -> None:
        raw = raw.strip()
        if not raw or raw.startswith("data:"):
            return
        full = urljoin(base, raw)
        if not full.lower().startswith(("http://", "https://")):
            return
        if full in seen:
            return
        seen.add(full)
        target.append(full)

    for node in tree.css("script[src]"):
        add(others, node.attributes.get("src") or "")
    for node in tree.css("link[rel]"):
        attrs = node.attributes
        if "stylesheet" in (attrs.get("rel") or "").lower():
            add(others, attrs.get("href") or "")

    total = len(images) + len(others)
    budget = max(0, limit)
    picked_images = images[:budget]
    rest = budget - len(picked_images)
    picked_others = others[:rest] if rest > 0 else []
    return picked_images, picked_others, total


def declared_width(node) -> int | None:
    attrs = node.attributes
    w = (attrs.get("width") or "").strip()
    if w.isdigit():
        return int(w)
    style = (attrs.get("style") or "").lower()
    m = re.search(r"width\s*:\s*(\d+)\s*px", style)
    if m:
        return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Ein Lead pruefen
# ---------------------------------------------------------------------------


def unbewertet(
    lead: Lead, final_url: str, status: str, grund: str, befund: str, now: str
) -> Result:
    """Ergebnis ohne Chaos-Score: Seite nicht erreichbar oder Pruefung gescheitert.

    Ohne Score und ohne Verkaufsargumente - die Befunde einer Seite, die sich
    nicht oeffnen laesst, waeren geraten.
    """
    res = Result(
        name=lead.name, url=lead.url, ort=lead.ort, branche=lead.branche,
        final_url=final_url, status=status, score=None, cms="", argumente=[],
        befunde=[befund], severities={}, geprueft_am=now,
        telefon=lead.telefon, email=lead.email, unerreichbar_grund=grund,
    )
    res.empfehlung, res.empfehlung_grund = naechster_schritt(res)
    return res


async def audit_lead(lead: Lead, fetcher: Fetcher, args: argparse.Namespace, psi_key: str | None) -> Result:
    now = datetime.now().isoformat(timespec="seconds")
    candidates = normalize_url(lead.url)
    if not candidates:
        return unbewertet(lead, "", STATUS_PRUEFFEHLER, "", "[Pruefung] Keine URL in der CSV", now)

    # robots.txt zuerst - gesperrte Seiten fassen wir nicht an.
    try:
        allowed = await fetcher.robots_allows(candidates[0])
    except Exception:  # noqa: BLE001
        allowed = True
    if not allowed:
        return unbewertet(
            lead, candidates[0], STATUS_UNERREICHBAR, "robots",
            "[Erreichbarkeit] Durch robots.txt fuer automatisierte Pruefung gesperrt, nicht bewertet", now,
        )

    fetched = await fetcher.get_page(candidates)

    # Seite nicht erreichbar: kein Score. Ein Timeout, ein DNS-Fehler oder ein
    # 5xx besagt nichts ueber die Qualitaet der Seite - und in den
    # Quelldaten stecken viele Tippfehler und tote Adressen, die sonst
    # mit Score 100 ganz oben landeten.
    if fetched.error:
        grund = grund_aus_text(fetched.error)
        return unbewertet(
            lead, fetched.final_url or candidates[0], STATUS_UNERREICHBAR, grund,
            f"[Erreichbarkeit] Seite nicht erreichbar: {fetched.error}", now,
        )
    if fetched.status is not None and fetched.status >= 500:
        return unbewertet(
            lead, fetched.final_url or candidates[0], STATUS_UNERREICHBAR, "http5xx",
            f"[Erreichbarkeit] Server antwortet mit HTTP {fetched.status}", now,
        )
    tree = None
    if not fetched.error and fetched.html:
        try:
            tree = LexborHTMLParser(fetched.html)
        except Exception:  # noqa: BLE001
            tree = None

    ctx = Context(fetched=fetched, tree=tree)

    if tree is not None:
        base = fetched.final_url or candidates[0]

        # Impressum / Datenschutz: nur die Links der Startseite pruefen
        imp, dat = find_legal_links(tree, base)
        legal = LegalInfo(impressum_url=imp, datenschutz_url=dat)
        if imp:
            legal.impressum_status, legal.impressum_html = await fetcher.status_und_text(imp)
        if dat and dat != imp:
            legal.datenschutz_status = await fetcher.head_status(dat)
        elif dat:
            legal.datenschutz_status = legal.impressum_status
        ctx.legal = legal

        # Assets vermessen
        if args.assets > 0:
            refs = bild_refs(tree, base)
            images, others, total_refs = collect_asset_urls(tree, base, args.assets)
            info = AssetInfo(refs=total_refs)
            masse: dict[str, tuple[int, int] | None] = {}
            # Alle Abrufe gleichzeitig anstossen: der Takt pro Host bleibt (der
            # HostLimiter reiht Abrufe an denselben Host im Sekundenabstand ein),
            # Dateien von verschiedenen Hosts - Seite, CDN - warten aber nicht
            # mehr aufeinander.
            alle = images + others
            abgerufen = await asyncio.gather(*(fetcher.measure_asset(u) for u in alle))
            for url, got in zip(alle, abgerufen):
                if got is None:
                    continue
                size, head = got
                info.measured += 1
                info.total_bytes += size
                if url in images:
                    masse[url] = image_dimensions(head)
                    if size > HUGE_IMAGE_BYTES:
                        info.oversized.append((url, size))
            # Die Anteilsaussage zaehlt nur Inhaltsbilder - auch solche, die
            # wegen des Abruflimits nicht vermessen wurden, sofern ihre Breite
            # aus dem srcset bekannt ist.
            info.images_measured, info.images_small = zaehle_inhaltsbilder(refs, masse)
            ctx.assets = info

        # PageSpeed Insights
        if args.pagespeed and psi_key:
            ctx.psi_score = await fetch_pagespeed(fetcher, base, psi_key)
            ctx.psi_ran = ctx.psi_score is not None

    results = [c(ctx) for c in CHECKS]

    # Liegt unter der Domain nur eine Platzhalterseite, sind alle weiteren
    # Befunde irrefuehrend: "kein Viewport" und "kein Impressum" beschreiben
    # dann die Seite des Providers, nicht die der Firma.
    substanz = next((r for r in results if r.key == "substanz"), None)
    if substanz is not None and substanz.severity == 1.0:
        results = [substanz] + [
            CheckResult(r.key, None) for r in results if r.key != "substanz"
        ]

    score = score_from_checks(results)
    arguments = build_arguments(results)
    _hits, cms = detect_stack(ctx)

    befunde: list[str] = []
    severities: dict[str, float | None] = {}
    for r in results:
        severities[r.key] = r.severity
        for f in r.findings:
            befunde.append(f"[{LABELS.get(r.key, r.key)}] {f.text}")

    status = STATUS_OK
    if not arguments and status == STATUS_OK:
        arguments = ["Keine groben Maengel gefunden - hier lohnt eher ein Gespraech ueber Inhalte als ueber Technik."]

    telefon = email = ""
    quelle_kontakt = ""
    formular = False
    if tree is not None:
        telefon, email = kontaktdaten(tree)
        if telefon or email:
            quelle_kontakt = "Startseite"
        formular = any(f.css("input, textarea") for f in tree.css("form"))

        # Steht nichts auf der Startseite, im Impressum nachsehen. Dort muss
        # in Deutschland ohnehin ein Kontaktweg stehen.
        if not (telefon and email) and ctx.legal and ctx.legal.impressum_html:
            try:
                imp_tree = LexborHTMLParser(ctx.legal.impressum_html)
            except Exception:  # noqa: BLE001
                imp_tree = None
            if imp_tree is not None:
                imp_tel, imp_mail = kontaktdaten(imp_tree)
                if imp_tel and not telefon:
                    telefon, quelle_kontakt = imp_tel, "Impressum"
                if imp_mail and not email:
                    email = imp_mail
                    quelle_kontakt = quelle_kontakt or "Impressum"

    # Aus der Quelle mitgebrachte Daten (OpenStreetMap) als naechster Rueckfall
    if not telefon and lead.telefon:
        telefon, quelle_kontakt = lead.telefon, quelle_kontakt or "OpenStreetMap"
    if not email and lead.email:
        email = lead.email
        quelle_kontakt = quelle_kontakt or "OpenStreetMap"

    # Immer noch keine Nummer: einmal im Branchenverzeichnis nachschlagen.
    # Genau hier sitzen die interessantesten Faelle - geparkte Domains und
    # Seiten ohne Impressum, bei denen sonst "Kontakt suchen" stehen bliebe.
    if not telefon and args.verzeichnis:
        treffer = await kontaktsuche.oertliche_suchen(lead.name, lead.ort, fetcher)
        if treffer:
            telefon = treffer.telefon
            quelle_kontakt = f"{treffer.quelle} ({treffer.name[:40]})"

    res = Result(
        name=lead.name,
        url=lead.url,
        ort=lead.ort,
        branche=lead.branche,
        final_url=fetched.final_url or candidates[0],
        status=status,
        score=score,
        cms=cms,
        argumente=arguments,
        befunde=befunde or ["Keine Auffaelligkeiten gefunden"],
        severities=severities,
        geprueft_am=now,
        pagespeed=ctx.psi_score,
        telefon=telefon,
        email=email,
        kontakt_quelle=quelle_kontakt,
        kontaktformular=formular,
    )
    res.empfehlung, res.empfehlung_grund = naechster_schritt(res)
    return res


async def fetch_pagespeed(fetcher: Fetcher, url: str, key: str) -> int | None:
    endpoint = "https://pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed"
    params = {"url": url, "strategy": "mobile", "category": "performance", "key": key}
    try:
        resp = await fetcher.secure.get(endpoint, params=params, timeout=90.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        raw = data["lighthouseResult"]["categories"]["performance"]["score"]
        return int(round(float(raw) * 100))
    except Exception:  # noqa: BLE001 - PSI darf den Lauf nie kippen
        return None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def url_key(url: str) -> str:
    """Einheitlicher Schluessel fuer Cache und Statusbuch.

    Schema und abschliessender Schraegstrich fallen weg, damit "firma.de",
    "https://firma.de" und "https://Firma.DE/" denselben Eintrag treffen.
    """
    k = url.strip().lower()
    for schema in ("https://", "http://"):
        if k.startswith(schema):
            k = k[len(schema):]
            break
    return k.rstrip("/")


class Cache:
    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path, timeout=15)
        # WAL, damit die GUI im Vordergrund lesen und schreiben kann, waehrend
        # im Hintergrund ein Lauf Ergebnisse in dieselbe Datei schreibt.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS audit ("
            " url_key TEXT PRIMARY KEY,"
            " fetched_at TEXT NOT NULL,"
            " with_pagespeed INTEGER NOT NULL DEFAULT 0,"
            " payload TEXT NOT NULL)"
        )
        self.conn.commit()

    @staticmethod
    def key(lead: Lead) -> str:
        return url_key(lead.url)

    def get(self, lead: Lead, max_age_days: int, want_pagespeed: bool) -> Result | None:
        row = self.conn.execute(
            "SELECT fetched_at, with_pagespeed, payload FROM audit WHERE url_key = ?",
            (self.key(lead),),
        ).fetchone()
        if not row:
            return None
        fetched_at, with_psi, payload = row
        if want_pagespeed and not with_psi:
            return None
        if max_age_days > 0:
            try:
                age = (datetime.now() - datetime.fromisoformat(fetched_at)).days
                if age > max_age_days:
                    return None
            except ValueError:
                return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        # Stammdaten aus der CSV gewinnen, die koennen sich geaendert haben
        data.update(name=lead.name, ort=lead.ort, branche=lead.branche, url=lead.url)
        return Result(**data)

    def put(self, result: Result, with_pagespeed: bool) -> None:
        payload = json.dumps(result.__dict__, ensure_ascii=False)
        self.conn.execute(
            "INSERT INTO audit (url_key, fetched_at, with_pagespeed, payload) VALUES (?,?,?,?) "
            "ON CONFLICT(url_key) DO UPDATE SET fetched_at=excluded.fetched_at,"
            " with_pagespeed=excluded.with_pagespeed, payload=excluded.payload",
            (url_key(result.url), result.geprueft_am, int(with_pagespeed), payload),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# Akquise-Status
# ---------------------------------------------------------------------------

# Reihenfolge ist die Reihenfolge im Auswahlfeld der GUI.
STATUS_WERTE = [
    "offen",
    "angerufen",
    "wiedervorlage",
    "termin",
    "angebot raus",
    "kunde",
    "kein Interesse",
    "nicht passend",
]


@dataclass
class LeadStatus:
    """Was nach dem Audit passiert ist. Haengt nicht am Pruefergebnis."""

    url_key: str
    status: str = "offen"
    notiz: str = ""
    wiedervorlage: str = ""  # ISO-Datum oder leer
    geaendert_am: str = ""

    @property
    def offen(self) -> bool:
        return self.status == "offen"


class Statusbuch:
    """Akquise-Status pro Lead, in derselben SQLite-Datei wie der Cache.

    Bewusst getrennt vom Audit-Cache: `--refresh` wirft Pruefergebnisse weg,
    aber niemals die Notiz, die nach einem Telefonat entstanden ist.
    """

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path, timeout=15)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS lead_status ("
            " url_key TEXT PRIMARY KEY,"
            " status TEXT NOT NULL DEFAULT 'offen',"
            " notiz TEXT NOT NULL DEFAULT '',"
            " wiedervorlage TEXT NOT NULL DEFAULT '',"
            " geaendert_am TEXT NOT NULL DEFAULT '')"
        )
        self.conn.commit()

    @staticmethod
    def key(url: str) -> str:
        return url_key(url)

    def get(self, url: str) -> LeadStatus:
        k = self.key(url)
        row = self.conn.execute(
            "SELECT status, notiz, wiedervorlage, geaendert_am FROM lead_status WHERE url_key = ?",
            (k,),
        ).fetchone()
        if not row:
            return LeadStatus(url_key=k)
        return LeadStatus(k, row[0], row[1], row[2], row[3])

    def alle(self) -> dict[str, LeadStatus]:
        rows = self.conn.execute(
            "SELECT url_key, status, notiz, wiedervorlage, geaendert_am FROM lead_status"
        ).fetchall()
        return {r[0]: LeadStatus(*r) for r in rows}

    def setzen(self, url: str, status: str, notiz: str = "", wiedervorlage: str = "") -> LeadStatus:
        k = self.key(url)
        jetzt = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO lead_status (url_key, status, notiz, wiedervorlage, geaendert_am)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(url_key) DO UPDATE SET status=excluded.status, notiz=excluded.notiz,"
            " wiedervorlage=excluded.wiedervorlage, geaendert_am=excluded.geaendert_am",
            (k, status, notiz, wiedervorlage, jetzt),
        )
        self.conn.commit()
        return LeadStatus(k, status, notiz, wiedervorlage, jetzt)

    def faellige_wiedervorlagen(self, stichtag: str | None = None) -> list[LeadStatus]:
        tag = stichtag or datetime.now().date().isoformat()
        rows = self.conn.execute(
            "SELECT url_key, status, notiz, wiedervorlage, geaendert_am FROM lead_status"
            " WHERE wiedervorlage != '' AND wiedervorlage <= ? ORDER BY wiedervorlage",
            (tag,),
        ).fetchall()
        return [LeadStatus(*r) for r in rows]

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# Ein-/Ausgabe
# ---------------------------------------------------------------------------


def read_leads(path: Path) -> list[Lead]:
    leads: list[Lead] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        if not reader.fieldnames:
            return leads
        cols = {(c or "").strip().lower(): (c or "") for c in reader.fieldnames}
        if "url" not in cols:
            raise SystemExit(f"CSV {path} hat keine Spalte 'url' (gefunden: {reader.fieldnames})")
        for row in reader:
            url = (row.get(cols["url"]) or "").strip()
            if not url:
                continue
            leads.append(
                Lead(
                    name=(row.get(cols.get("name", ""), "") or "").strip() or url,
                    url=url,
                    ort=(row.get(cols.get("ort", ""), "") or "").strip(),
                    branche=(row.get(cols.get("branche", ""), "") or "").strip(),
                    telefon=(row.get(cols.get("telefon", ""), "") or "").strip(),
                    email=(row.get(cols.get("email", ""), "") or "").strip(),
                )
            )
    return leads


CSV_COLUMNS = [
    "score", "empfehlung", "name", "telefon", "email", "ort", "branche", "url",
    "final_url", "status", "cms", "pagespeed", "akquise", "wiedervorlage",
    "notiz", "begruendung", "argumente", "befunde", "geprueft_am",
]


def write_csv(results: list[Result], path: Path, status: dict[str, LeadStatus] | None = None) -> None:
    status = status or {}
    sev_cols = [f"s_{k}" for k in WEIGHTS]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(CSV_COLUMNS + sev_cols)
        for r in results:
            st = status.get(Statusbuch.key(r.url), LeadStatus(url_key=""))
            writer.writerow(
                [
                    "" if r.score is None else f"{r.score:.1f}", r.empfehlung, r.name, r.telefon, r.email,
                    r.ort, r.branche, r.url, r.final_url,
                    r.status, r.cms, "" if r.pagespeed is None else r.pagespeed,
                    st.status, st.wiedervorlage, st.notiz, r.empfehlung_grund,
                    " ".join(r.argumente), " | ".join(r.befunde), r.geprueft_am,
                ]
                + ["" if r.severities.get(k) is None else f"{r.severities[k]:.2f}" for k in WEIGHTS]
            )


def write_html(
    results: list[Result], path: Path, source: str, status: dict[str, LeadStatus] | None = None
) -> None:
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report.html.j2")
    status = status or {}
    rows = []
    for r in results:
        st = status.get(Statusbuch.key(r.url), LeadStatus(url_key=""))
        rows.append({
            "name": r.name, "url": r.url, "final_url": r.final_url, "ort": r.ort,
            "branche": r.branche, "score": r.score, "status": r.status, "cms": r.cms,
            "pagespeed": r.pagespeed, "argumente": r.argumente, "befunde": r.befunde,
            "akquise": st.status, "notiz": st.notiz, "wiedervorlage": st.wiedervorlage,
        })
    html = template.render(
        rows=rows,
        orte=sorted({r.ort for r in results if r.ort}),
        branchen=sorted({r.branche for r in results if r.branche}),
        erstellt=datetime.now().strftime("%d.%m.%Y %H:%M"),
        quelle=source,
        gewichte=WEIGHTS,
        labels=LABELS,
    )
    path.write_text(html, encoding="utf-8")


def print_table(results: list[Result]) -> None:
    table = Table(title="site-audit - Relaunch-Kandidaten", header_style="bold")
    table.add_column("Score", justify="right", width=6)
    table.add_column("Name", max_width=28)
    table.add_column("Ort", max_width=14)
    table.add_column("Technik", max_width=22)
    table.add_column("Status", width=8)
    table.add_column("Wichtigster Befund", max_width=52)

    for r in results:
        color = "dim" if r.score is None else "red" if r.score >= 60 else "yellow" if r.score >= 35 else "green"
        first = r.argumente[0] if r.argumente else (r.befunde[0] if r.befunde else "")
        table.add_row(
            f"[{color}]{'-' if r.score is None else format(r.score, '.0f')}[/{color}]",
            r.name, r.ort, r.cms or "-", r.status, first
        )
    console.print(table)


def load_env_file(path: Path) -> None:
    """.env einlesen, ohne extra Abhaengigkeit. Echte Umgebung gewinnt."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Lauf
# ---------------------------------------------------------------------------


def anzeige_score(score: float | None) -> str:
    return "    -" if score is None else f"{score:5.1f}"


def sortierschluessel(r: Result) -> tuple[bool, float, str]:
    """Schlechteste zuerst, nicht bewertete Seiten ans Ende."""
    return (r.score is None, -(r.score or 0.0), r.name.lower())


async def run(
    leads: list[Lead],
    args: argparse.Namespace,
    cache: Cache | None,
    psi_key: str | None,
    melde: "Callable[[int, int, Result, bool], None] | None" = None,
    abbruch: "Callable[[], bool] | None" = None,
) -> list[Result]:
    """Alle Leads pruefen.

    `melde(fertig, gesamt, ergebnis, aus_cache)` wird nach jedem Lead gerufen.
    Ohne Callback zeichnet die Funktion selbst einen rich-Fortschritt - die GUI
    uebergibt stattdessen ihren eigenen und bekommt so keine Terminalausgabe.
    `abbruch()` wird vor jedem Lead geprueft; liefert es True, bricht der Lauf
    kontrolliert ab und gibt zurueck, was bis dahin fertig war.
    """
    limiter = HostLimiter(args.host_interval)
    fetcher = Fetcher(args.timeout, limiter, args.concurrency)
    sem = asyncio.Semaphore(args.concurrency)
    results: list[Result] = []
    gesamt = len(leads)

    progress = None
    task = None
    if melde is None:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        progress.start()
        task = progress.add_task("Pruefe Seiten", total=gesamt)

    def fertig(res: Result, aus_cache: bool) -> None:
        results.append(res)
        if progress is not None:
            if aus_cache:
                progress.console.log(f"[dim]cache[/dim]  {anzeige_score(res.score)}  {res.name}")
            else:
                farbe = ("dim" if res.score is None else "red" if res.score >= 60
                         else "yellow" if res.score >= 35 else "green")
                progress.console.log(
                    f"[{farbe}]{anzeige_score(res.score)}[/{farbe}]  {res.name}  [dim]{res.cms or res.status}[/dim]"
                )
            progress.advance(task)
        else:
            melde(len(results), gesamt, res, aus_cache)

    async def worker(lead: Lead) -> None:
        if abbruch is not None and abbruch():
            return
        if cache is not None and not args.refresh:
            cached = cache.get(lead, args.max_age, args.pagespeed)
            if cached is not None:
                fertig(cached, True)
                return
        async with sem:
            if abbruch is not None and abbruch():
                return
            try:
                res = await audit_lead(lead, fetcher, args, psi_key)
            except Exception as exc:  # noqa: BLE001 - nie den ganzen Lauf kippen
                res = unbewertet(
                    lead, "", STATUS_PRUEFFEHLER, "",
                    f"[Pruefung] Interner Fehler: {type(exc).__name__}: {exc}",
                    datetime.now().isoformat(timespec="seconds"),
                )
        if cache is not None:
            cache.put(res, args.pagespeed)
        fertig(res, False)

    try:
        await asyncio.gather(*(worker(lead) for lead in leads))
    finally:
        if progress is not None:
            progress.stop()
        await fetcher.aclose()

    results.sort(key=sortierschluessel)
    return results


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="audit.py",
        description="Firmenwebseiten auf Relaunch-Bedarf pruefen und nach Chaos-Score sortieren.",
    )
    p.add_argument("csv", type=Path, help="Leads-CSV mit den Spalten name,url,ort,branche")
    p.add_argument("--out", default="ergebnis", help="Basisname der Ausgabe (ergebnis -> ergebnis.csv/.html)")
    p.add_argument("--pagespeed", action="store_true", help="PageSpeed Insights mitpruefen (braucht PSI_KEY)")
    p.add_argument("--refresh", action="store_true", help="Cache ignorieren und alles neu pruefen")
    p.add_argument("--cache", type=Path, default=Path(".site-audit-cache.sqlite"), help="Pfad der Cache-Datenbank")
    p.add_argument("--no-cache", action="store_true", help="Cache gar nicht benutzen")
    p.add_argument("--max-age", type=int, default=0, help="Cache-Eintraege aelter als N Tage neu pruefen (0 = nie)")
    p.add_argument(
        "--concurrency", type=int, default=MAX_PARALLEL,
        help=f"Wie viele Seiten gleichzeitig (Vorgabe {MAX_PARALLEL}); der Takt pro Host bleibt bei 1 Request/s",
    )
    p.add_argument("--host-interval", type=float, default=HOST_INTERVAL, help="Sekunden zwischen Requests pro Host")
    p.add_argument("--assets", type=int, default=DEFAULT_ASSETS, help="Wie viele Bilder/CSS/JS pro Seite vermessen (0 = aus)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Timeout pro Request in Sekunden")
    p.add_argument(
        "--kein-verzeichnis",
        dest="verzeichnis",
        action="store_false",
        help="nicht im Branchenverzeichnis nach fehlenden Telefonnummern nachschlagen",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_file(Path(".env"))

    if not args.csv.exists():
        console.print(f"[red]CSV nicht gefunden:[/red] {args.csv}")
        return 2

    leads = read_leads(args.csv)
    if not leads:
        console.print(f"[red]Keine Leads in {args.csv} gefunden.[/red]")
        return 2

    psi_key = None
    if args.pagespeed:
        psi_key = os.environ.get("PSI_KEY", "").strip() or None
        if not psi_key:
            console.print("[yellow]PSI_KEY nicht gesetzt - PageSpeed-Check wird uebersprungen.[/yellow]")

    cache = None if args.no_cache else Cache(args.cache)
    # Das Statusbuch wird immer geoeffnet, auch bei --no-cache: es haelt
    # Gespraechsnotizen, nicht Pruefergebnisse.
    statusbuch = Statusbuch(args.cache)

    console.print(
        f"[bold]site-audit[/bold]  {len(leads)} Leads aus {args.csv}  "
        f"(max {args.concurrency} parallel, {args.host_interval:g}s pro Host)"
    )

    try:
        results = asyncio.run(run(leads, args, cache, psi_key))
    finally:
        if cache is not None:
            cache.close()

    out = str(args.out)
    for suffix in (".csv", ".html"):
        if out.endswith(suffix):
            out = out[: -len(suffix)]
    csv_path = Path(out + ".csv")
    html_path = Path(out + ".html")
    status = statusbuch.alle()
    write_csv(results, csv_path, status)
    write_html(results, html_path, str(args.csv), status)

    console.print()
    print_table(results)
    console.print(f"\n[green]Geschrieben:[/green] {csv_path}  und  {html_path}")

    faellig = statusbuch.faellige_wiedervorlagen()
    if faellig:
        console.print(f"\n[yellow]{len(faellig)} Wiedervorlage(n) faellig:[/yellow]")
        for st in faellig[:10]:
            console.print(f"  {st.wiedervorlage}  {st.url_key}  [dim]{st.notiz[:60]}[/dim]")
    statusbuch.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
