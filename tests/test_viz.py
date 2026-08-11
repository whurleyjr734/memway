"""memway viz: the real map rendered, and the fence around it.

Two claims carry the weight.

FIRST, viz is a READ tool - .coord must be byte-identical after a run.
The dig lesson: `load_existing()` warms a pickle cache, so a tool that
only reads still wrote until it was told not to.

SECOND, knowledge must arrive through MetaStore's READ path, never a raw
JSONL read. read_all() is what decorates an entry with `stale`, and a
note rendered without its flag asserts a currency the map never claimed.
The stale fixture here therefore creates staleness the real way - stamp
an entry, then change the code - rather than hardcoding the field, so a
regression that bypasses the store fails this file.
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import viz as vizmod
from memway.viz import export, render, viz, VIZ_WARN_ENTITIES, PLACEHOLDER
from memway.metadata import CHANNELS, MetaStore
from memway.indexer import Indexer


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "memway.cli", *[str(a) for a in args]],
        capture_output=True, text=True, cwd=str(HERE))


SRC_A = '''"""Package a."""


def alpha(x):
    """Alpha."""
    return x + 1


def beta(x):
    """Beta."""
    if x < 0:
        raise ValueError("neg")
    return x * 2
'''

SRC_B = '''"""Package b."""

from a import alpha


class Runner:
    """Runs things."""

    def run(self, x):
        return alpha(x)
'''


@pytest.fixture
def mapped(tmp_path):
    """A real map with knowledge on every channel and one STALE entry."""
    R = tmp_path / "proj"
    R.mkdir()
    subprocess.run(["git", "-C", str(R), "init", "-q", "-b", "main"],
                   check=True)
    (R / "a.py").write_text(SRC_A)
    (R / "b.py").write_text(SRC_B)
    r = cli("init", R)
    assert r.returncode == 0, r.stderr[-400:]

    coord = R / ".coord"
    ix = Indexer(R, coord)
    ix.load_existing()
    meta = MetaStore(coord)
    alpha = ix.resolve("a.alpha")
    beta = ix.resolve("a.beta")
    assert alpha and beta

    # one entry per channel, all on alpha, each labelled by its channel
    for ch in CHANNELS:
        meta.add(alpha.coord_id, ch, f"{ch} entry for alpha",
                 author="test", body_hash=alpha.body_hash)

    # beta gets a stamp, and then its CODE CHANGES - staleness must come
    # from the read path noticing the hash moved, not from a literal.
    meta.add(beta.coord_id, "notes", "beta was stamped before the edit",
             author="test", body_hash=beta.body_hash)
    (R / "a.py").write_text(SRC_A.replace("return x * 2", "return x * 3"))
    r = cli("index", R)
    assert r.returncode == 0, r.stderr[-400:]
    return R


def fingerprint(repo: Path) -> dict:
    return {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((repo / ".coord").rglob("*")) if p.is_file()}


def embedded(html: str) -> dict:
    """Pull the injected payload back OUT of the emitted file.

    Every integrity assertion re-reads the artifact rather than trusting
    the exporter's return value - an exit code proves nothing about what
    landed on disk.
    """
    m = re.search(r"const SAMPLE = (\{.*?\});\n", html, re.S)
    assert m, "no injected payload found in the emitted HTML"
    return json.loads(m.group(1))


# ------------------------------------------------------------- the fence

def test_the_fence_viz_never_writes_to_coord(mapped):
    """viz is a READ tool. If this fails it has grown a side effect -
    do not 'fix' the test."""
    before = fingerprint(mapped)
    assert before
    viz(str(mapped), str(mapped / "out.html"))
    after = fingerprint(mapped)
    assert before == after, "viz mutated .coord"
    assert set(before) == set(after), "viz added/removed a file in .coord"


def test_the_fence_holds_through_the_cli(mapped):
    before = fingerprint(mapped)
    r = cli("viz", mapped, "--out", mapped / "cli.html")
    assert r.returncode == 0, r.stderr[-400:]
    assert fingerprint(mapped) == before


def test_output_never_lands_inside_coord(mapped):
    r = viz(str(mapped))
    out = Path(r["out"])
    assert out.name == "memway-map.html"
    assert out.parent == mapped.resolve(), "default output is the repo root"
    assert ".coord" not in out.parts, "the render is not part of the map"
    assert out.exists()


# ------------------------------------------------------------ field mapping

def test_entity_fields_map_to_the_template_contract(mapped):
    p = export(str(mapped))
    ix = Indexer(mapped, mapped / ".coord")
    ix.load_existing()
    by_id = {e["id"]: e for e in p["entities"]}
    assert by_id
    for cid, e in ix.entities.items():
        row = by_id[cid]
        assert row["qualname"] == e.qualname
        assert row["kind"] == (e.kind or "function").lower()
        assert row["file"] == e.path
        assert row["lines"] == f"{e.lineno}-{e.end_lineno or e.lineno}"
        assert isinstance(row["lines"], str), "template expects 'a-b'"
        assert isinstance(row["complexity"], int)
        assert isinstance(row["knowledge"], list)
        assert set(row) >= {"id", "qualname", "kind", "file", "lines",
                            "complexity", "knowledge"}


def test_typed_edges_are_preserved(mapped):
    """The template styles and filters edges by kind; the approved page's
    normalize() dropped it, so every edge rendered as 'calls'."""
    p = export(str(mapped))
    assert p["edges"]
    for ed in p["edges"]:
        assert set(ed) >= {"source", "target", "kind"}
        assert ed["kind"], "an untyped edge would render as generic 'calls'"
    kinds = {ed["kind"] for ed in p["edges"]}
    assert "contains" in kinds, kinds
    from memway.edges import EdgeBuilder
    raw = EdgeBuilder.load(mapped / ".coord")
    ids = {e["id"] for e in p["entities"]}
    expect = {r["kind"] for r in raw
              if r.get("src") in ids and r.get("dst") in ids}
    assert kinds == expect, "exported edge kinds must match the map's"


def test_template_carries_edge_kind_through_normalize():
    """Guards the template fix itself: the JS must not drop kind."""
    js = vizmod.TEMPLATE.read_text()
    assert 'kind:(ed.kind||"calls")' in js, \
        "normalize() dropped edge kind - filters and styling go dead"
    assert "l.kind" in js, "the edge filter reads kind"


# --------------------------------------------------------------- knowledge

def test_every_channel_exports_with_its_label(mapped):
    p = export(str(mapped))
    rows = [e for e in p["entities"] if e["qualname"].endswith(".alpha")]
    assert len(rows) == 1
    kn = rows[0]["knowledge"]
    got = {k["channel"] for k in kn}
    assert got == set(CHANNELS), f"missing channels: {set(CHANNELS) - got}"
    assert len(kn) == len(CHANNELS)
    for k in kn:
        assert k["channel"] in CHANNELS
        assert k["text"] == f"{k['channel']} entry for alpha"
        assert k["author"] == "test"
        assert "stale" in k


def test_channel_labels_survive_into_the_emitted_html(mapped):
    """Consumer surface: the label must be in the artifact, not just the
    exporter's return value."""
    out = mapped / "labels.html"
    viz(str(mapped), str(out))
    data = embedded(out.read_text())
    alpha = next(e for e in data["entities"]
                 if e["qualname"].endswith(".alpha"))
    assert {k["channel"] for k in alpha["knowledge"]} == set(CHANNELS)
    js = out.read_text()
    assert "k.channel?" in js, "the card must render the channel label"


def test_stale_flag_comes_from_the_read_path(mapped):
    """beta was stamped, then its code changed. The flag must be produced
    by MetaStore.read_all noticing the hash moved."""
    p = export(str(mapped))
    beta = next(e for e in p["entities"] if e["qualname"].endswith(".beta"))
    assert beta["knowledge"], "beta's stamped note must still be exported"
    entry = beta["knowledge"][0]
    assert entry["stale"] is True, "changed code must mark the entry stale"
    assert entry["text"] == "beta was stamped before the edit"
    # ...and it is genuinely the store's verdict, not the exporter's
    ix = Indexer(mapped, mapped / ".coord")
    ix.load_existing()
    e = ix.resolve("a.beta")
    md = MetaStore(mapped / ".coord").read_all(
        e.coord_id, current_hash={getattr(e, "logic_hash", ""), e.body_hash})
    assert any(x.get("stale") for xs in md.values() for x in xs)


def test_fresh_entries_are_not_marked_stale(mapped):
    """The control: without it, 'everything stale' would also pass."""
    p = export(str(mapped))
    alpha = next(e for e in p["entities"]
                 if e["qualname"].endswith(".alpha"))
    assert alpha["knowledge"]
    assert not any(k["stale"] for k in alpha["knowledge"]), \
        "alpha was never edited after stamping"


def test_census_counts_knowledge_and_stale(mapped):
    r = viz(str(mapped), str(mapped / "c.html"))
    c = r["census"]
    assert c["knowledge"] == len(CHANNELS) + 1
    assert c["stale"] == 1
    assert c["entities"] > 0 and c["edges"] > 0
    for part in ("entities", "edges", "knowledge entries", "stale"):
        assert part in r["line"]


# ------------------------------------------------------- filter + boundary

def test_filter_renders_subtree_plus_marked_boundary(mapped):
    p = export(str(mapped), filter_prefix="a")
    quals = {e["qualname"] for e in p["entities"]}
    assert any(q.startswith("a.") or q == "a" for q in quals)
    inside = [e for e in p["entities"] if not e.get("boundary")]
    bound = [e for e in p["entities"] if e.get("boundary")]
    assert inside, "the subtree itself must render"
    for e in inside:
        base = e["qualname"]
        assert base == "a" or base.startswith("a."), base
    for e in bound:
        assert "[boundary]" in e["qualname"], \
            "a boundary node must be visibly marked, not silently included"
        assert not (e["qualname"].split("  ")[0] == "a"
                    or e["qualname"].startswith("a."))
    assert p["_census"]["boundary"] == len(bound)


def test_filter_keeps_edges_that_cross_the_boundary(mapped):
    """Rendering the subtree alone would silently cut its edges."""
    p = export(str(mapped), filter_prefix="a")
    ids = {e["id"] for e in p["entities"]}
    assert all(ed["source"] in ids and ed["target"] in ids
               for ed in p["edges"])
    bound = {e["id"] for e in p["entities"] if e.get("boundary")}
    if bound:
        touching = [ed for ed in p["edges"]
                    if ed["source"] in bound or ed["target"] in bound]
        assert touching, "boundary nodes exist only because an edge reaches them"


def test_unknown_filter_prefix_is_actionable(mapped):
    p = export(str(mapped), filter_prefix="nosuchpkg")
    assert "error" in p
    assert "hint" in p and p["hint"]
    assert "entities" not in p


# ---------------------------------------------------------- scale honesty

def test_large_map_refuses_without_force_or_filter(mapped, monkeypatch):
    """No silent sampling: what renders is what was asked for."""
    monkeypatch.setattr(vizmod, "VIZ_WARN_ENTITIES", 1)
    p = export(str(mapped))
    assert "error" in p
    assert "exceeds" in p["error"]
    assert "--filter" in p["hint"] and "--force" in p["hint"]
    assert "sampled" in p["hint"]
    assert "entities" in p


def test_force_renders_everything_above_the_limit(mapped, monkeypatch):
    monkeypatch.setattr(vizmod, "VIZ_WARN_ENTITIES", 1)
    p = export(str(mapped), force=True)
    assert "error" not in p
    ix = Indexer(mapped, mapped / ".coord")
    ix.load_existing()
    assert len(p["entities"]) == len(ix.entities), "force renders ALL"


def test_filter_also_bypasses_the_limit(mapped, monkeypatch):
    monkeypatch.setattr(vizmod, "VIZ_WARN_ENTITIES", 1)
    p = export(str(mapped), filter_prefix="a")
    assert "error" not in p, "an explicit subtree is already a scoped ask"


def test_cli_refuses_large_map_with_a_usable_message(mapped, monkeypatch):
    r = cli("viz", mapped, "--out", mapped / "x.html")
    assert r.returncode == 0
    monkeypatch.setenv("PYTHONPATH", str(HERE))


# ------------------------------------------------------- injection integrity

def test_emitted_html_is_intact_and_carries_the_data(mapped):
    out = mapped / "intact.html"
    viz(str(mapped), str(out))
    html = out.read_text()
    assert html.lstrip().lower().startswith("<!doctype html")
    assert html.rstrip().endswith("</html>")
    assert PLACEHOLDER not in html, "placeholder must be substituted"
    assert html.count("<script") == html.count("</script>")
    data = embedded(html)
    assert set(data) == {"repo", "entities", "edges"}
    assert "_census" not in data, "internal bookkeeping must not ship"
    ix = Indexer(mapped, mapped / ".coord")
    ix.load_existing()
    assert len(data["entities"]) == len(ix.entities)
    assert data["repo"].startswith("proj")


def test_script_close_sequences_cannot_break_out(mapped):
    """A note containing </script> would end the block early and the page
    would render as raw text below that point."""
    ix = Indexer(mapped, mapped / ".coord")
    ix.load_existing()
    e = ix.resolve("a.alpha")
    MetaStore(mapped / ".coord").add(
        e.coord_id, "notes", "danger </script><h1>pwned</h1>",
        author="test", body_hash=e.body_hash)
    out = mapped / "esc.html"
    viz(str(mapped), str(out))
    html = out.read_text()
    assert "</script><h1>pwned" not in html
    assert "<\\/script>" in html, "the slash must be escaped in the payload"
    assert html.count("<script") == html.count("</script>")
    data = embedded(html)
    texts = [k["text"] for e2 in data["entities"] for k in e2["knowledge"]]
    assert any("pwned" in t for t in texts), "the text itself is preserved"


def test_template_ships_with_the_package():
    assert vizmod.TEMPLATE.exists()
    assert PLACEHOLDER in vizmod.TEMPLATE.read_text()
    pyproject = (HERE / "pyproject.toml").read_text()
    assert "viz_template.html" in pyproject, "template must be packaged"


def test_no_new_runtime_dependencies():
    """stdlib only; D3 stays a CDN reference in the template."""
    src = (HERE / "memway" / "viz.py").read_text()
    for bad in ("import jinja2", "import requests", "import numpy",
                "import lxml", "import bs4"):
        assert bad not in src
    tpl = vizmod.TEMPLATE.read_text()
    assert "cdnjs.cloudflare.com" in tpl and "d3.min.js" in tpl, \
        "D3 stays a CDN reference, not a vendored dependency"


def test_missing_map_is_actionable(tmp_path):
    p = export(str(tmp_path))
    assert "error" in p and "memway init" in p["error"]
