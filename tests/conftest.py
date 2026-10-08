"""Gemeinsame Hilfen fuer die Tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from selectolax.lexbor import LexborHTMLParser

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import audit  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_context(
    name: str | None = None,
    *,
    html: str | None = None,
    url: str = "https://beispiel.de/",
    status: int = 200,
    headers: dict[str, str] | None = None,
    error: str | None = None,
    tls_problem: str | None = None,
    redirects: int = 0,
    elapsed: float = 0.4,
    scheme_downgrade: bool = False,
    assets: audit.AssetInfo | None = None,
    legal: audit.LegalInfo | None = None,
    psi_score: int | None = None,
) -> audit.Context:
    """Context ohne Netz bauen - aus einer Fixture oder aus HTML direkt."""
    if html is None:
        html = load_fixture(name) if name else ""

    fetched = audit.Fetched(
        url=url,
        final_url="" if error else url,
        status=None if error else status,
        headers={k.lower(): v for k, v in (headers or {}).items()},
        html="" if error else html,
        html_bytes=len(html.encode("utf-8")),
        redirects=redirects,
        elapsed=elapsed,
        error=error,
        tls_problem=tls_problem,
        scheme_downgrade=scheme_downgrade,
    )
    tree = None if error or not html else LexborHTMLParser(html)

    if legal is None and tree is not None:
        imp, dat = audit.find_legal_links(tree, url)
        legal = audit.LegalInfo(impressum_url=imp, datenschutz_url=dat)

    return audit.Context(
        fetched=fetched,
        tree=tree,
        assets=assets,
        legal=legal,
        psi_score=psi_score,
        psi_ran=psi_score is not None,
    )


def severity(ctx: audit.Context, key: str) -> float | None:
    """Einen einzelnen Check laufen lassen und nur die Severity zurueckgeben."""
    return run_check(ctx, key).severity


def run_check(ctx: audit.Context, key: str) -> audit.CheckResult:
    for check in audit.CHECKS:
        result = check(ctx)
        if result.key == key:
            return result
    raise AssertionError(f"Check {key!r} gibt es nicht")


def findings_text(ctx: audit.Context, key: str) -> str:
    return " ".join(f.text for f in run_check(ctx, key).findings).lower()


@pytest.fixture
def baukasten() -> audit.Context:
    return make_context("baukasten_alt.html")


@pytest.fixture
def modern() -> audit.Context:
    return make_context("modern_sauber.html")


@pytest.fixture
def flash() -> audit.Context:
    return make_context("flash_frames.html")


@pytest.fixture
def wp_alt() -> audit.Context:
    return make_context("wordpress_alt.html")
