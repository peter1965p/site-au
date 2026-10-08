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

MAX_PARALLEL = 10  # gleichzeitig geprüfte Seiten
HOST_INTERVAL = 1.0  # Sekunden Mindestabstand zwischen Requests pro Host
DEFAULT_TIMEOUT = 20.0
RETRIES = 3            # Versuche pro Kandidaten-URL bei Netzfehlern
RETRY_BACKOFF = 1.5    # Sekunden, waechst mit jedem Fehlversuch
DEFAULT_ASSETS = 15  # wie viele Bilder/CSS/JS pro Seite vermessen werden
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
    images_measured: int = 0
    images_small: int = 0


@dataclass
class LegalInfo:
    impressum_url: str | None = None
    impressum_status: int | None = None
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


@dataclass
class Result:
    name: str
    url: str
    ort: str
    branche: str
    final_url: str
    status: str  # ok | fehler | gesperrt
    score: float
    cms: str
    argumente: list[str]
    befunde: list[str]
    severities: dict[str, float | None]
    geprueft_am: str
    pagespeed: int | None = None


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
            return seen.verify_message or str(seen)
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


def human_bytes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1000:
        return f"{n / 1000:.0f} kB"
    return f"{n} B"


# ---------------------------------------------------------------------------
# Checks - jeder nimmt den Context und liefert ein CheckResult
# ---------------------------------------------------------------------------


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
        hits.append(
            (
                "Slider Revolution",
                0.5,
                "Das Slider-Revolution-Plugin ist eingebunden - in alten Versionen eine bekannte Sicherheitsluecke.",
            )
        )

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
    findings = [Finding(name, arg) for name, _sev, arg in hits if _sev >= 0.3 or arg]
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

    if a.images_measured >= 3:
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
    def __init__(self, timeout: float, limiter: HostLimiter) -> None:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9",
        }
        limits = httpx.Limits(max_connections=MAX_PARALLEL * 3, max_keepalive_connections=10)
        self.secure = httpx.AsyncClient(
            headers=headers, timeout=timeout, follow_redirects=True, limits=limits, verify=True
        )
        self.insecure = httpx.AsyncClient(
            headers=headers, timeout=timeout, follow_redirects=True, limits=limits, verify=False
        )
        self.limiter = limiter
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
        except Exception:
            try:
                await self.limiter.wait(host)
                resp = await self.insecure.get(base + "/robots.txt")
            except Exception:
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
            tls_problem = None
            resp = None
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
                    if attempt < RETRIES - 1:
                        await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
            if resp is None:
                continue

            elapsed = time.monotonic() - started
            body = resp.text
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


def collect_asset_urls(tree: LexborHTMLParser, base: str, limit: int) -> tuple[list[str], list[str], int]:
    """Bilder zuerst, danach CSS/JS. Rueckgabe: (bilder, sonstige, gesamt_refs)."""
    images: list[str] = []
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

    for node in tree.css("img"):
        attrs = node.attributes
        src = attrs.get("src") or attrs.get("data-src") or ""
        if not src:
            srcset = attrs.get("srcset") or ""
            if srcset:
                src = srcset.split(",")[0].strip().split(" ")[0]
        add(images, src)
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


async def audit_lead(lead: Lead, fetcher: Fetcher, args: argparse.Namespace, psi_key: str | None) -> Result:
    now = datetime.now().isoformat(timespec="seconds")
    candidates = normalize_url(lead.url)
    if not candidates:
        return Result(
            lead.name, lead.url, lead.ort, lead.branche, "", "fehler", 100.0, "",
            ["Es ist keine URL hinterlegt."], ["Keine URL in der CSV"], {}, now,
        )

    # robots.txt zuerst - gesperrte Seiten fassen wir nicht an.
    try:
        allowed = await fetcher.robots_allows(candidates[0])
    except Exception:  # noqa: BLE001
        allowed = True
    if not allowed:
        return Result(
            lead.name, lead.url, lead.ort, lead.branche, candidates[0], "gesperrt", 0.0, "",
            ["Automatische Pruefung per robots.txt untersagt - Seite manuell ansehen."],
            ["Durch robots.txt fuer automatisierte Pruefung gesperrt, nicht bewertet"],
            {}, now,
        )

    fetched = await fetcher.get_page(candidates)
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
            legal.impressum_status = await fetcher.head_status(imp)
        if dat and dat != imp:
            legal.datenschutz_status = await fetcher.head_status(dat)
        elif dat:
            legal.datenschutz_status = legal.impressum_status
        ctx.legal = legal

        # Assets vermessen
        if args.assets > 0:
            images, others, total_refs = collect_asset_urls(tree, base, args.assets)
            info = AssetInfo(refs=total_refs)
            for url in images + others:
                got = await fetcher.measure_asset(url)
                if got is None:
                    continue
                size, head = got
                info.measured += 1
                info.total_bytes += size
                is_image = url in images
                if is_image:
                    info.images_measured += 1
                    dims = image_dimensions(head)
                    if dims and dims[0] < SMALL_IMAGE_PX:
                        info.images_small += 1
                    if size > HUGE_IMAGE_BYTES:
                        info.oversized.append((url, size))
            ctx.assets = info

        # PageSpeed Insights
        if args.pagespeed and psi_key:
            ctx.psi_score = await fetch_pagespeed(fetcher, base, psi_key)
            ctx.psi_ran = ctx.psi_score is not None

    results = [c(ctx) for c in CHECKS]
    score = score_from_checks(results)
    arguments = build_arguments(results)
    _hits, cms = detect_stack(ctx)

    befunde: list[str] = []
    severities: dict[str, float | None] = {}
    for r in results:
        severities[r.key] = r.severity
        for f in r.findings:
            befunde.append(f"[{LABELS.get(r.key, r.key)}] {f.text}")

    status = "fehler" if fetched.error else "ok"
    if not arguments and status == "ok":
        arguments = ["Keine groben Maengel gefunden - hier lohnt eher ein Gespraech ueber Inhalte als ueber Technik."]

    return Result(
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
    )


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
                )
            )
    return leads


CSV_COLUMNS = [
    "score", "name", "ort", "branche", "url", "final_url", "status", "cms",
    "pagespeed", "akquise", "wiedervorlage", "notiz", "argumente", "befunde",
    "geprueft_am",
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
                    f"{r.score:.1f}", r.name, r.ort, r.branche, r.url, r.final_url,
                    r.status, r.cms, "" if r.pagespeed is None else r.pagespeed,
                    st.status, st.wiedervorlage, st.notiz,
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
        color = "red" if r.score >= 60 else "yellow" if r.score >= 35 else "green"
        if r.status == "gesperrt":
            color = "dim"
        first = r.argumente[0] if r.argumente else (r.befunde[0] if r.befunde else "")
        table.add_row(
            f"[{color}]{r.score:.0f}[/{color}]", r.name, r.ort, r.cms or "-", r.status, first
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
    fetcher = Fetcher(args.timeout, limiter)
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
                progress.console.log(f"[dim]cache[/dim]  {res.score:5.1f}  {res.name}")
            else:
                farbe = "red" if res.score >= 60 else "yellow" if res.score >= 35 else "green"
                progress.console.log(
                    f"[{farbe}]{res.score:5.1f}[/{farbe}]  {res.name}  [dim]{res.cms or res.status}[/dim]"
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
                res = Result(
                    lead.name, lead.url, lead.ort, lead.branche, "", "fehler", 100.0, "",
                    ["Die Pruefung ist abgebrochen, Seite manuell ansehen."],
                    [f"[Pruefung] Interner Fehler: {type(exc).__name__}: {exc}"],
                    {}, datetime.now().isoformat(timespec="seconds"),
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

    results.sort(key=lambda r: (-r.score, r.name.lower()))
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
    p.add_argument("--concurrency", type=int, default=MAX_PARALLEL, help="Wie viele Seiten gleichzeitig")
    p.add_argument("--host-interval", type=float, default=HOST_INTERVAL, help="Sekunden zwischen Requests pro Host")
    p.add_argument("--assets", type=int, default=DEFAULT_ASSETS, help="Wie viele Bilder/CSS/JS pro Seite vermessen (0 = aus)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Timeout pro Request in Sekunden")
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
