"""Tests zum Tempo: tote Adressen, Timeouts, gleichzeitige Abrufe, Parallelitaet.

Die Netzzugriffe laufen ueber httpx.MockTransport - es geht nichts ins Netz.
"""

from __future__ import annotations

import argparse
import asyncio
import time

import httpx
import pytest

import audit
from audit import Fetched, Lead, Fetcher, HostLimiter, ist_dauerhaft_tot

DNS_FEHLER = "[Errno -2] Name or service not known"


def _fetcher(handler, parallel: int = 10) -> tuple[Fetcher, list[str]]:
    """Fetcher, dessen Netz durch `handler` ersetzt ist. Zweite Rueckgabe: Aufrufprotokoll."""
    protokoll: list[str] = []

    def mit_protokoll(request: httpx.Request) -> httpx.Response:
        protokoll.append(f"{request.method} {request.url}")
        return handler(request)

    f = Fetcher(5.0, HostLimiter(0.0), parallel)
    f.secure = httpx.AsyncClient(transport=httpx.MockTransport(mit_protokoll), follow_redirects=True)
    f.insecure = httpx.AsyncClient(transport=httpx.MockTransport(mit_protokoll), follow_redirects=True)
    return f, protokoll


@pytest.fixture(autouse=True)
def _kein_backoff(monkeypatch):
    """Die Wartezeit zwischen Wiederholungen ist nicht Gegenstand dieser Tests."""
    monkeypatch.setattr(audit, "RETRY_BACKOFF", 0.0)


# ===========================================================================
# Tote Adressen
# ===========================================================================


@pytest.mark.parametrize(
    "text, tot",
    [
        ("Verbindung fehlgeschlagen ([Errno -2] Name or service not known)", True),
        ("[Errno -5] No address associated with hostname", True),
        ("nodename nor servname provided, or not known", True),
        ("[Errno -3] Temporary failure in name resolution", False),  # Aussetzer, darf wiederholt werden
        ("[Errno 111] Connection refused", False),
        ("Timeout beim Laden", False),
    ],
)
def test_nur_endgueltige_dns_fehler_gelten_als_tot(text, tot):
    assert ist_dauerhaft_tot(text) is tot


async def test_dns_fehler_wird_nicht_wiederholt_und_http_nicht_versucht():
    """Frueher: 3 Versuche, dann dasselbe nochmal unter http://. Bei einer Adresse,
    die es nicht gibt, sind das vier verlorene Wartezeiten."""

    def handler(_req):
        raise httpx.ConnectError(DNS_FEHLER)

    f, protokoll = _fetcher(handler)
    seite = await f.get_page(["https://tot.example", "http://tot.example"])
    assert seite.error and "Name or service not known" in seite.error
    assert len(protokoll) == 1, protokoll
    await f.aclose()


async def test_robots_merkt_sich_den_toten_host():
    """Scheitert schon robots.txt an DNS, kostet der Seitenabruf nichts mehr."""

    def handler(_req):
        raise httpx.ConnectError(DNS_FEHLER)

    f, protokoll = _fetcher(handler)
    assert await f.robots_allows("https://tot.example/") is True  # keine Regeln = erlaubt
    nach_robots = len(protokoll)
    seite = await f.get_page(["https://tot.example", "http://tot.example"])
    assert seite.error
    assert len(protokoll) == nach_robots, "kein weiterer Netzzugriff nach robots.txt"
    await f.aclose()


async def test_robots_bei_timeout_versucht_nicht_noch_unsicher():
    """Der unsichere Zweitversuch hilft nur bei Zertifikatsproblemen. Bei Timeout
    verdoppelte er die Wartezeit, ohne etwas zu bringen."""

    def handler(_req):
        raise httpx.ConnectTimeout("zu langsam")

    f, protokoll = _fetcher(handler)
    await f.robots_allows("https://langsam.example/")
    assert len(protokoll) == 1
    await f.aclose()


async def test_robots_bei_zertifikatsproblem_versucht_unsicher_nochmal():
    import ssl

    def secure_handler(_req):
        raise httpx.ConnectError("x", request=None) from ssl.SSLCertVerificationError("certificate has expired")

    f, protokoll = _fetcher(lambda _r: httpx.Response(200, text="User-agent: *\nDisallow:"))
    f.secure = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: (protokoll.append("secure"), secure_handler(r))[1])
    )
    await f.robots_allows("https://abgelaufen.example/")
    assert "secure" in protokoll
    assert any(p.startswith("GET https://abgelaufen.example/robots.txt") for p in protokoll)
    await f.aclose()


# ===========================================================================
# Timeouts
# ===========================================================================


async def test_timeout_wird_nur_einmal_wiederholt():
    def handler(_req):
        raise httpx.ReadTimeout("keine Antwort")

    f, protokoll = _fetcher(handler)
    seite = await f.get_page(["https://langsam.example"])
    assert "Timeout" in seite.error
    assert len(protokoll) == audit.TIMEOUT_VERSUCHE, protokoll
    assert audit.TIMEOUT_VERSUCHE < audit.RETRIES
    await f.aclose()


async def test_sonstige_netzfehler_werden_weiter_dreimal_versucht():
    """Ein Verbindungsabbruch ist oft nur ein Aussetzer - der darf eine Seite
    nicht als nicht erreichbar abstempeln."""
    zaehler = {"n": 0}

    def handler(_req):
        zaehler["n"] += 1
        if zaehler["n"] < 3:
            raise httpx.ConnectError("[Errno 104] Connection reset by peer")
        return httpx.Response(200, text="<html><body>da</body></html>")

    f, protokoll = _fetcher(handler)
    seite = await f.get_page(["https://wackelig.example"])
    assert seite.error is None and seite.status == 200
    assert len(protokoll) == 3
    await f.aclose()


async def test_http_ausweichversuch_bleibt_bei_verweigerter_verbindung():
    """Nur-http-Seiten muessen weiter erreichbar bleiben."""

    def handler(req):
        if req.url.scheme == "https":
            raise httpx.ConnectError("[Errno 111] Connection refused")
        return httpx.Response(200, text="<html><body>nur http</body></html>")

    f, _p = _fetcher(handler)
    seite = await f.get_page(["https://alt.example", "http://alt.example"])
    assert seite.error is None
    assert seite.final_url.startswith("http://")
    assert seite.scheme_downgrade
    await f.aclose()


# ===========================================================================
# Impressum mit einem Abruf
# ===========================================================================


async def test_status_und_text_ist_ein_einziger_abruf():
    f, protokoll = _fetcher(lambda _r: httpx.Response(200, text="<html>Impressum Firma</html>"))
    status, text = await f.status_und_text("https://firma.example/impressum")
    assert (status, "Impressum Firma" in text) == (200, True)
    assert len(protokoll) == 1, "vorher HEAD und GET: zwei Abrufe, zwei Sekunden Takt"
    await f.aclose()


async def test_status_und_text_liefert_bei_404_keinen_text():
    f, _p = _fetcher(lambda _r: httpx.Response(404, text="Seite nicht gefunden"))
    assert await f.status_und_text("https://firma.example/impressum") == (404, "")
    await f.aclose()


async def test_status_und_text_faellt_bei_netzfehler_nicht_um():
    def handler(_req):
        raise httpx.ConnectError("weg")

    f, _p = _fetcher(handler)
    assert await f.status_und_text("https://firma.example/impressum") == (None, "")
    await f.aclose()


# ===========================================================================
# audit_lead: Impressum und Assets
# ===========================================================================

ARGS = argparse.Namespace(assets=6, pagespeed=False, verzeichnis=False)

SEITE = """<html><head><title>Firma Beispiel - Handwerk in der Eifel</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Beispiel."></head><body><main><h1>Firma</h1>
<p>Wir sind ein Handwerksbetrieb in der Eifel und bieten Ihnen alles rund um Haus und Hof. Rufen Sie uns an,
wir beraten Sie gern und kommen zu Ihnen. Seit vielen Jahren arbeiten wir zuverlaessig und zu fairen Preisen.</p>
{bilder}</main><footer><a href="/impressum">Impressum</a> <a href="/datenschutz">Datenschutz</a></footer></body></html>"""


class ZeitFetcher:
    """Fetcher-Attrappe, die Asset-Abrufe verzoegert, um Gleichzeitigkeit zu messen."""

    def __init__(self, html: str, dauer: float = 0.3):
        self.html = html
        self.dauer = dauer
        self.aufrufe: list[str] = []

    async def robots_allows(self, _url):
        return True

    async def get_page(self, kandidaten):
        return Fetched(url=kandidaten[0], final_url=kandidaten[0] + "/", status=200, html=self.html,
                       html_bytes=len(self.html))

    async def head_status(self, url):
        self.aufrufe.append(f"head {url}")
        return 200

    async def status_und_text(self, url):
        self.aufrufe.append(f"status_und_text {url}")
        return 200, ""

    async def text_of(self, url):
        self.aufrufe.append(f"text_of {url}")
        return ""

    async def measure_asset(self, url):
        await asyncio.sleep(self.dauer)
        return 1000, b""


async def test_impressum_wird_mit_einem_abruf_geholt():
    f = ZeitFetcher(SEITE.format(bilder=""))
    await audit.audit_lead(Lead("Firma", "firma.example"), f, ARGS, None)
    impressum = [a for a in f.aufrufe if "impressum" in a]
    assert impressum == ["status_und_text https://firma.example/impressum"]


async def test_assets_werden_nebenlaeufig_abgerufen():
    """Sechs Dateien a 0,3 s: nacheinander 1,8 s, gleichzeitig ~0,3 s. Der Takt pro
    Host bleibt Sache des HostLimiters, nicht der Reihenfolge im Code."""
    bilder = "".join(f'<img src="/foto{i}.jpg" srcset="/foto{i}.jpg 800w" alt="Bild {i}">' for i in range(6))
    f = ZeitFetcher(SEITE.format(bilder=bilder), dauer=0.3)
    t = time.monotonic()
    await audit.audit_lead(Lead("Firma", "firma.example"), f, ARGS, None)
    dauer = time.monotonic() - t
    assert dauer < 1.2, f"{dauer:.2f} s - die Abrufe laufen offenbar hintereinander"


async def test_asset_budget_wird_eingehalten():
    bilder = "".join(f'<img src="/foto{i}.jpg" alt="Bild {i}">' for i in range(20))
    f = ZeitFetcher(SEITE.format(bilder=bilder), dauer=0.0)
    gemessen: list[str] = []
    orig = f.measure_asset

    async def zaehlen(url):
        gemessen.append(url)
        return await orig(url)

    f.measure_asset = zaehlen
    await audit.audit_lead(Lead("Firma", "firma.example"), f, argparse.Namespace(assets=6, pagespeed=False, verzeichnis=False), None)
    assert len(gemessen) == 6


# ===========================================================================
# Einstellungen
# ===========================================================================


def test_vorgaben_sind_auf_tempo_ausgelegt():
    assert audit.MAX_PARALLEL == 30
    assert audit.DEFAULT_ASSETS == 6
    assert audit.CONNECT_TIMEOUT < audit.DEFAULT_TIMEOUT


def test_host_takt_bleibt_bei_einer_sekunde():
    """Die Hoeflichkeit gegenueber den Seiten darf durch das Tempo nicht leiden."""
    assert audit.HOST_INTERVAL == 1.0
    assert audit.build_parser().parse_args(["x.csv"]).host_interval == 1.0


def test_verbindungslimit_waechst_mit_der_parallelitaet(monkeypatch):
    """Ein zu kleiner Pool laeuft in einen Pool-Timeout, der faelschlich als
    "nicht erreichbar" gemeldet wuerde."""
    gesehen: list[httpx.Limits] = []
    echt = httpx.Limits

    def merken(**kw):
        lim = echt(**kw)
        gesehen.append(lim)
        return lim

    monkeypatch.setattr(audit.httpx, "Limits", merken)
    Fetcher(5.0, HostLimiter(0.0), parallel=50)
    assert gesehen and gesehen[0].max_connections >= 50 * 3


async def test_host_takt_gilt_auch_bei_gleichzeitigen_abrufen():
    """Gleichzeitig angestossene Abrufe an denselben Host werden trotzdem im Takt eingereiht."""
    limiter = HostLimiter(interval=0.2)
    start = time.monotonic()
    await asyncio.gather(*(limiter.wait("gleicher-host.example") for _ in range(4)))
    assert time.monotonic() - start >= 0.6
