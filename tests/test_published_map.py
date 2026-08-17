"""The published page must describe the map that is committed beside it.

WHY THIS EXISTS. `.coord` rides every commit - the pre-commit hook
indexes and stages it, and every commit's embedded map hashes to that
commit's own tree. `docs/map.html` is a RENDERED artifact and nothing
kept it in step, so it drifted **nine releases**: last rendered at
abc5cf7, it was still being served while v0.54.3 through v0.56.1 shipped.

What that looked like on the live site, for `memway.parsers`:

    memway/parsers.py:1-731    complexity 143
    confirm: "PARSE_SCHEMA_VERSION is 6, not 5"   - badged FRESH

All three wrong. The constant had been 7 since 0.56.0, that entry was
stale AND superseded in the live map, and the confirm which said so was
absent from the page entirely. A superseded note presented as current,
with a freshness badge earned against a hash that moved nine releases
ago - memway's own promise inverted on its own shop window.

The same root as CLAUDE.md lesson 10: a derived artifact with no
automation keeping it honest, discovered by a human looking at a screen.

RELEASE-GATED, NOT PER-SAVE. `.coord` changes on essentially every
commit, so asserting this on every run would keep the suite red for
ordinary work and train everyone to ignore it - the failure mode this
project keeps rediscovering. It is a gate you run at release, next to
the corpus floors, and CLAUDE.md's close ceremony says to run it.

    pytest -m release
"""

import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

pytestmark = pytest.mark.release

PUBLISHED = HERE / "docs" / "map.html"


def _payload(html: str) -> dict:
    """The render payload the page actually carries."""
    i = html.index("const SAMPLE = ")
    j = html.index("\n", i)
    return json.loads(html[i + len("const SAMPLE = "):j].rstrip().rstrip(";"))


def _live_counts():
    from memway.viz import export
    p = export(str(HERE))
    assert "error" not in p, p
    return len(p["entities"]), len(p["edges"])


def test_the_published_map_matches_the_committed_one():
    """Counts, not bytes. A byte comparison would fail on anything
    cosmetic in the template and teach people to regenerate blindly;
    entity and edge counts move when and only when the map does."""
    if not PUBLISHED.exists():
        pytest.skip("no docs/map.html in this checkout")

    page = _payload(PUBLISHED.read_text())
    page_ents, page_edges = len(page["entities"]), len(page["edges"])
    live_ents, live_edges = _live_counts()

    assert (page_ents, page_edges) == (live_ents, live_edges), (
        f"docs/map.html describes {page_ents} entities / {page_edges} edges "
        f"but the committed map has {live_ents} / {live_edges}. The page is "
        f"stale - regenerate it before the release:\n"
        f"    memway viz . --out docs/map.html --force")


def test_the_published_map_shows_no_answered_knowledge_as_fresh():
    """The specific lie that was live for nine releases: an entry that
    somebody had already superseded, rendered without its flag.

    Not a count - a shape. Every rendered entry must carry `superseded`,
    and no entry may claim to be both the newest in its channel and one
    that a later entry replaced.
    """
    if not PUBLISHED.exists():
        pytest.skip("no docs/map.html in this checkout")

    page = _payload(PUBLISHED.read_text())
    missing, contradictory = [], []
    for e in page["entities"]:
        seen_newest = set()
        for k in e.get("knowledge", []):
            if "superseded" not in k:
                missing.append(e.get("qualname"))
                continue
            ch = k.get("channel")
            if not k["superseded"]:
                if ch in seen_newest:
                    contradictory.append(f"{e.get('qualname')} [{ch}]")
                seen_newest.add(ch)

    assert not missing, (
        f"entries rendered without a superseded flag: {missing[:5]} - the "
        f"page cannot distinguish history from a live warning")
    assert not contradictory, (
        f"two unsuperseded entries in one channel: {contradictory[:5]} - "
        f"only the newest per channel may read as current")


def test_the_render_summary_counts_the_decisive_queue():
    """The line a human reads at render time said "81 stale" on a repo
    whose decisive queue was zero. The rings were right; the number was
    raw. Fourth appearance of that shape, so it gets a pin."""
    import ast
    src = (HERE / "memway" / "viz.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "export")
    dump = ast.dump(fn)
    assert "unsuperseded_stale" in dump, \
        "the render summary no longer asks the ring rule"
    assert not re.search(r'if\s+k\[["\']stale["\']\]', src), \
        "viz counts raw stale entries again - ask unsuperseded_stale"
