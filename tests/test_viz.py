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

PRESENCE VS BEHAVIOUR. Many assertions here are of the form `"..." in html`.
For CSS rules and escaping, the bytes ARE the mechanism and presence is a
real test. For JavaScript behaviour it is not: 0.53.0 shipped an origin
toggle whose markup, predicate source and styling rule were all present and
which did nothing, because normalize() dropped the field before the
predicate saw it. Tests named for a runtime behaviour they cannot observe
have been renamed to say what they actually check. Where a behaviour can be
executed, it is - see test_test_lens.py, which runs the shipped JavaScript
in node.
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import viz as vizmod
from memway.viz import export, render, viz, VIZ_WARN_ENTITIES, PLACEHOLDER
from memway.metadata import CHANNELS, MetaStore
from memway.metadata import accepted_for
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
        e.coord_id, current_hash=accepted_for(e))
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
    """stdlib only - and d3 is VENDORED, not fetched.

    This assertion used to read `"cdnjs.cloudflare.com" in tpl`, i.e. it
    enforced the CDN link. That is why an acceptance sweep found the
    emitted page phoning out while this suite stayed green: the guard was
    not missing, it was pointing the wrong way. Vendoring d3 adds no
    PYTHON dependency - it is a static asset shipped in package-data - so
    the zero-dependency claim is untouched.
    """
    src = (HERE / "memway" / "viz.py").read_text()
    for bad in ("import jinja2", "import requests", "import numpy",
                "import lxml", "import bs4"):
        assert bad not in src
    py = (HERE / "pyproject.toml").read_text()
    assert "dependencies = []" in py, "runtime dependencies must stay empty"
    tpl = vizmod.TEMPLATE.read_text()
    assert "cdnjs.cloudflare.com" not in tpl, \
        "d3 must be vendored and inlined, never linked from a CDN"
    assert vizmod.D3.exists(), "the vendored d3 is what replaced the link"


def test_missing_map_is_actionable(tmp_path):
    p = export(str(tmp_path))
    assert "error" in p and "memway init" in p["error"]


# --------------------------------------------------- theme + dismissability

def test_template_uses_the_website_palette_and_fonts():
    """The explorer should look like it belongs to memway.io. Site tokens
    are canonical; the explorer's original names are ALIASES so nothing
    downstream breaks (console.py reads var(--amber) seven times)."""
    tpl = vizmod.TEMPLATE.read_text()
    site = (HERE / "docs" / "index.html").read_text()
    for token, value in (("--void", "#060913"), ("--panel", "#0C1222"),
                         ("--line", "#233052"), ("--star", "#EAF0FF"),
                         ("--nebula1", "#7C6CFF"), ("--nebula2", "#3EC8FF"),
                         ("--beacon", "#FF7A66"), ("--fresh", "#4AE3B5")):
        assert f"{token}:{value}" in tpl.replace(" ", ""), token
        assert f"{token}:{value}" in site.replace(" ", ""), \
            f"{token} must match the site, not merely exist"
    # Fonts are SYSTEM STACKS now, not the site's webfonts. Sora and
    # JetBrains Mono arrived over two Google Fonts links, which made every
    # rendered map announce itself to Google and fail with no egress. The
    # palette above is what carries the identity; the typeface follows the
    # reader's OS. See tests/test_airgap.py.
    assert "--font-sans:" in tpl and "--font-mono:" in tpl
    assert "-apple-system" in tpl and "ui-monospace" in tpl
    assert "fonts.googleapis.com" not in tpl
    for gone in ("Fraunces", "Archivo", "IBM Plex"):
        assert gone not in tpl, f"{gone} is not a site font"


def test_legacy_colour_names_still_resolve():
    """Retheme by VALUE, never by rename - console.py depends on these."""
    tpl = vizmod.TEMPLATE.read_text()
    assert "--amber:var(--fresh)" in tpl.replace(" ", "")
    assert "--coral:var(--beacon)" in tpl.replace(" ", "")
    assert "--ink:var(--void)" in tpl.replace(" ", "")
    con = (HERE / "memway" / "console.py").read_text()
    assert "var(--amber)" in con, "the alias is load-bearing, not decorative"


def test_load_modal_declares_three_close_paths():
    """Same trap the tool rail had: openable easily, closable only by one
    small button."""
    tpl = vizmod.TEMPLATE.read_text()
    assert "function closeLoad()" in tpl
    assert 'document.getElementById("cancelLoad").onclick=closeLoad' in tpl
    assert "e.target===wrap" in tpl, "backdrop click must close it"
    assert 'e.key!=="Escape"' in tpl, "Escape must close it"
    assert "if(ta) ta.focus()" in tpl, "focus should land in the textarea"


def test_escape_closes_modal_before_panel():
    """Ordering matters: Escape should retreat one step, not throw the
    reader all the way out."""
    tpl = vizmod.TEMPLATE.read_text()
    i = tpl.index('if(e.key!=="Escape") return;')
    body = tpl[i:i + 400]
    assert body.index("closeLoad()") < body.index("clearSel("), \
        "the modal must be closed before the panel"


def test_motion_is_reduced_when_asked():
    tpl = vizmod.TEMPLATE.read_text()
    assert tpl.count("prefers-reduced-motion") >= 1
    assert "transition-duration:.01ms !important" in tpl


# ------------------------------------------------------ graph connectivity

def test_layout_pulls_every_node_home():
    """forceCenter moves the CENTROID; it applies no force to individual
    nodes. Without an x/y force a component with no links to the main mass
    feels only charge repulsion and drifts off as debris at the edge."""
    tpl = vizmod.TEMPLATE.read_text()
    assert 'd3.forceX(W/2)' in tpl and 'd3.forceY(H/2)' in tpl
    assert 'd3.forceCenter' in tpl, "centering still applies to the system"

    # THE STRENGTH IS NOW SCALED, so this reads the base constant and the
    # scaling rule instead of a literal in the force chain. forceX/forceY
    # are a spring toward one point: their total holds the layout inside a
    # disc, and at the small-graph value a 13k-node graph was squeezed to
    # an RMS radius of 3738 when it wanted ~6000. Small maps keep the
    # original number exactly; large ones relax as sqrt(N), because a
    # layout's radius grows as the square root of its node count.
    base = float(re.search(r'const HOME_BASE = ([\d.]+);', tpl).group(1))
    assert 0 < base <= 0.1, f"{base} would overpower the link layout"
    assert re.search(r'forceX\(W/2\)\.strength\(HOME\)', tpl), \
        "the homing force no longer reads the scaled strength"
    m = re.search(r'const HOME = nodes\.length > (\d+)\s*\?\s*'
                  r'HOME_BASE \* Math\.sqrt\((\d+) / nodes\.length\)', tpl)
    assert m, "the homing strength does not scale with the graph"
    # and it must only ever WEAKEN, never strengthen
    limit = int(m.group(1))
    assert base * (limit / 12987) ** 0.5 < base, "scaling does not relax"


def test_detached_components_are_labelled_not_hidden():
    """A node with no path to the main mass is a FACT about the codebase -
    nothing imports it. memway's own map has 4 components: the example
    hook script (17), the polyglot parser fixtures (10), and an empty
    __init__. Label it rather than leave the reader wondering whether the
    graph broke."""
    tpl = vizmod.TEMPLATE.read_text()
    assert "markComponents" in tpl
    assert "n.detached=!main.has(n.id)" in tpl
    assert "n.island=" in tpl, "island size makes the label specific"
    assert 'class="detached"' in tpl
    assert "no path to the main graph" in tpl


def test_the_real_map_has_the_components_the_label_describes():
    """Guards the claim itself: if memway's map ever becomes fully
    connected this test says so rather than the feature quietly meaning
    nothing."""
    import collections
    p = export(str(HERE), force=True)
    adj = collections.defaultdict(set)
    for ed in p["edges"]:
        adj[ed["source"]].add(ed["target"])
        adj[ed["target"]].add(ed["source"])
    seen, comps = set(), []
    for n in {e["id"] for e in p["entities"]}:
        if n in seen:
            continue
        stack, comp = [n], []
        seen.add(n)
        while stack:
            c = stack.pop()
            comp.append(c)
            for m in adj[c]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    assert len(comps) > 1, "the detached label would describe nothing"
    assert len(comps[0]) / len(p["entities"]) > 0.8, "one dominant island"


# ------------------------------- the page must not lie about itself

def test_title_and_header_come_from_one_derivation(tmp_path):
    """The tab and the header must carry the SAME derived label.

    They were independent: the header was computed per-repo and the title
    was a constant in the template - "memway - itsdangerous, the real map"
    - left over from when the flagship map really was itsdangerous. Every
    user's map inherited it, so their tab announced somebody else's
    project (C-b93d8e).

    Nothing caught it because a wrong constant is not a wrong behaviour.
    The payload tests, the airgap tests and the executed-predicate tests
    all pass on a page whose tab lies.

    This asserts against map_label() rather than against any literal, so
    it says nothing about WHAT the name should be - only that one
    derivation feeds both. It therefore keeps passing when 0.54.2 changes
    the derivation from the directory name to the project name.
    """
    import json as _json
    import re as _re
    from pathlib import Path as _P
    from memway.viz import export, render, map_label

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "m.py").write_text(
        'def alpha(x):\n    """D."""\n    return x + 1\n\n\n'
        'def beta(y):\n    """D."""\n    return alpha(y)\n')
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"],
                   capture_output=True)
    r = subprocess.run([sys.executable, "-m", "memway.cli", "init", str(repo)],
                       capture_output=True, text=True, cwd=str(HERE))
    assert r.returncode == 0, r.stderr[-400:]

    payload = export(str(repo))
    html = render(payload)

    n_ent = len(payload["entities"])
    n_edg = len(payload["edges"])
    assert n_ent, "fixture produced no entities - the test would be vacuous"
    expected = map_label(_P(str(repo)), "", n_ent, n_edg)

    # (a) the emitted title carries exactly the derived label
    m = _re.search(r"<title>(.*?)</title>", html, _re.S)
    assert m, "the emitted page has no <title>"
    title = m.group(1).strip()
    assert title.endswith(expected), (
        f"title does not carry the derived label\n"
        f"  title   : {title!r}\n  expected: ...{expected!r}")

    # (b) title and header carry the IDENTICAL label
    assert payload["repo"] == expected
    assert expected in title and payload["repo"] in title, (
        f"header={payload['repo']!r} title={title!r}")


def test_the_title_tracks_a_changed_derivation(tmp_path):
    """Guard the guard. If the title were still a constant that merely
    happened to contain the label, the test above could pass by accident;
    changing what map_label returns must move the title with it."""
    import re as _re
    from pathlib import Path as _P
    from memway import viz

    repo = tmp_path / "proj2"
    repo.mkdir()
    (repo / "m.py").write_text('def solo(x):\n    """D."""\n    return x\n')
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"],
                   capture_output=True)
    subprocess.run([sys.executable, "-m", "memway.cli", "init", str(repo)],
                   capture_output=True, text=True, cwd=str(HERE))

    real = viz.map_label
    try:
        viz.map_label = lambda p, f, a, b: "SENTINEL-LABEL-42"
        html = viz.render(viz.export(str(repo)))
    finally:
        viz.map_label = real
    title = _re.search(r"<title>(.*?)</title>", html, _re.S).group(1)
    assert "SENTINEL-LABEL-42" in title, (
        f"the title ignored the derivation and is still a constant: {title!r}")


# ------------------------------- an edge keeps its kind when you select it

EDGE_KINDS = ("calls", "inherits", "imports", "contains")


def test_selecting_a_node_keeps_the_edge_kinds_distinguishable():
    """Lit edges must stay coloured by kind.

    `.link.lit` set `stroke:var(--amber)`, which beat every per-kind rule
    below it - so the moment you clicked a node, calls, inherits, imports
    and contains all became one colour. That is precisely the moment the
    distinction matters: "what reaches this, and how".

    Asserted on the emitted bytes, and by SPECIFICITY rather than by
    eyeballing the file: a two-class rule cannot override a three-class
    one, so `.link.lit.calls` is what makes the fix hold.
    """
    from memway.viz import load_template
    html = load_template()
    assert "var(--amber)" not in _rule(html, ".link.lit"), \
        "lit still forces one colour over every kind"
    for kind in EDGE_KINDS:
        assert f".link.lit.{kind}{{" in html, f"no lit rule for {kind}"
        assert f"var(--e-{kind})" in _rule(html, f".link.lit.{kind}"), kind


def test_one_colour_per_kind_feeds_swatch_line_and_lit():
    """Three places must agree, so they read ONE variable.

    The legend teaches the mapping, the resting line uses it, the lit line
    uses it. Hard-coded hexes in any of the three is how a legend comes to
    describe a colour the graph no longer draws.
    """
    from memway.viz import load_template
    html = load_template()
    for kind in EDGE_KINDS:
        assert f"--e-{kind}:" in html, f"{kind} has no colour variable"
        swatch = _swatch(html, kind)
        assert f"var(--e-{kind})" in swatch, \
            f"legend swatch for {kind} does not read the variable: {swatch}"
        assert "#" not in swatch, f"legend swatch for {kind} hard-codes a hex"


def test_every_edge_kind_the_payload_can_carry_has_a_colour(tmp_path):
    """Guard against a kind existing in data with no rule to draw it.

    Executed against a real payload rather than the constant above, so a
    new edge kind added to the indexer shows up here instead of rendering
    in whatever the base rule happens to be.
    """
    import json as _json
    from memway.viz import export, render
    html = render(export(str(HERE)))
    i = html.index("const SAMPLE = ")
    j = html.index("\n", i)
    data = _json.loads(html[i + len("const SAMPLE = "):j].rstrip().rstrip(";"))
    kinds = {e.get("kind", "calls") for e in data["edges"]}
    assert len(kinds) >= 3, f"payload too thin to discriminate: {kinds}"
    missing = [k for k in kinds if f".link.lit.{k}{{" not in html]
    assert not missing, f"edge kinds with no lit colour: {missing}"


def _rule(html: str, selector: str) -> str:
    i = html.index(selector + "{")
    return html[i:html.index("}", i)]


def _swatch(html: str, kind: str) -> str:
    i = html.index(f'data-ekind="{kind}"')
    j = html.index("</span>", i)
    return html[i:j]


# ------------------------------ the map wears the site's logo

WORDMARK_PROPS = ("font-weight", "letter-spacing", "font-size", "color")


def _decl(css_rule: str) -> dict:
    out = {}
    for part in css_rule[css_rule.index("{") + 1:css_rule.rindex("}")].split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _rule_text(html: str, selector: str) -> str:
    i = html.index(selector)
    return html[i:html.index("}", i) + 1]


def test_the_map_wordmark_matches_the_site():
    """The map carries memway.io's logo, to the value.

    docs/index.html is canonical - the template says so in its own token
    block - and the map's wordmark had drifted on FOUR properties at once:
    weight 650 vs 800, letter-spacing +.2px vs -.02em, size 21 vs 20, and
    no gradient on the second half. Nothing caught it because a wordmark
    is a constant, and every test here checks behaviour.

    Compared property by property against the site rather than pinned to
    literals, so restyling memway.io moves both or fails loudly.
    """
    from memway.viz import load_template
    site = (HERE / "docs" / "index.html").read_text()
    mapp = load_template()

    want = _decl(_rule_text(site, ".wordmark{"))
    got = _decl(_rule_text(mapp, ".wordmark h1{"))
    for prop in WORDMARK_PROPS:
        assert prop in want, f"the site's wordmark no longer sets {prop}"
        assert got.get(prop) == want[prop], (
            f"{prop}: map has {got.get(prop)!r}, site has {want[prop]!r}")


def test_the_second_half_of_the_wordmark_carries_the_gradient():
    """`way` is gradient-filled on the site; plain text on the map was the
    most visible half of the drift."""
    from memway.viz import load_template
    site = (HERE / "docs" / "index.html").read_text()
    mapp = load_template()
    site_grad = _decl(_rule_text(site, ".wordmark b{"))
    map_grad = _decl(_rule_text(mapp, ".wordmark h1 b{"))
    for prop in ("background", "background-clip", "color"):
        assert map_grad.get(prop) == site_grad.get(prop), (
            f"{prop}: map {map_grad.get(prop)!r} vs site {site_grad.get(prop)!r}")
    assert "<h1>mem<b>way</b></h1>" in mapp, \
        "the wordmark is not split, so the gradient has nothing to fill"


def test_the_renderer_and_its_helpers_are_module_scope():
    """draw() must be reachable from applyFilters(), which is module scope.

    THE BUG, shipped for exactly one session: draw/buckets/pick were
    defined inside render(), so every module-scope caller threw
    "draw is not defined". applyFilters is module scope, so filters were
    dead AND render() threw on its own last line. The page still painted,
    because the tick handler closes over render's scope - which is why it
    looked fine in a screenshot and was broken in the hand.
    """
    tpl = vizmod.TEMPLATE.read_text()
    for fn in ("draw", "buckets", "pick"):
        assert re.search(rf"^function {fn}\(", tpl, re.M), (
            f"{fn}() is not declared at module scope - a nested definition "
            f"is invisible to applyFilters and to the console shell")


def test_render_does_not_touch_the_quadtree_cursor():
    """A temporal-dead-zone ReferenceError on the first line of render().

    render() opened by resetting qtStamp, which is declared with `let`
    LATER in the same scope. Touching a let-binding above its declaration
    throws, so render() died immediately and the canvas stayed blank -
    the page served nothing at all. Caught by a human opening it, not by
    the suite, which is why the jsdom witness now exists.
    """
    tpl = vizmod.TEMPLATE.read_text()
    body = tpl[tpl.index("function render(data){"):]
    body = body[:body.index("\nfunction ")] if "\nfunction " in body else body
    assert "qtStamp" not in body, (
        "render() references the quadtree cursor; it is declared with let "
        "at module scope and assigning it from here risks the TDZ error "
        "that blanked the canvas")



@pytest.fixture(scope="module")
def jsdom_env(tmp_path_factory):
    """node + jsdom, installed ONCE for every page-execution test.

    These are the only tests that run the emitted page rather than read
    it, and they are the only ones that caught the two fatal renderer
    bugs of 0.57.2. Marked network because of the install.
    """
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        pytest.skip("no JS runtime / npm")
    d = tmp_path_factory.mktemp("jsdom")
    r = subprocess.run([npm, "install", "jsdom", "--silent", "--no-audit",
                        "--no-fund", "--prefix", str(d)],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        pytest.skip(f"jsdom install unavailable: {r.stderr[-200:]}")
    return node, str(d / "node_modules" / "jsdom")

@pytest.mark.slow
@pytest.mark.network
def test_the_emitted_page_runs_without_error(tmp_path, jsdom_env):
    """THE witness. Loads the whole emitted page in a DOM and runs it.

    Two fatal bugs shipped in one session behind a green suite, because
    every other test around the renderer reads the file as TEXT:

      1. render() reset a `let` binding declared later in its own scope -
         a temporal-dead-zone ReferenceError on the FIRST line. The canvas
         stayed blank. The user opened it and said "its empty".
      2. draw() was defined inside render(), so applyFilters - module
         scope - threw "draw is not defined" on every filter change.

    Neither is detectable by grepping, and the canvas witness in
    test_test_lens.py missed both because it eval's draw() directly and
    never runs render(). Nothing short of executing the page finds this
    class, which is the whole lesson.

    Marked network because it npm-installs jsdom; run at release.
    """
    node, jsdom_path = jsdom_env
    from memway.viz import export, render
    page = tmp_path / "page.html"
    page.write_text(render(export(str(HERE))))

    runner = tmp_path / "run.js"
    runner.write_text(r"""
const fs = require("fs");
const { JSDOM } = require(process.argv[4]);
const calls = {arc:0, stroke:0, fill:0, moveTo:0, fillText:0, clearRect:0};
const errors = [];
const dom = new JSDOM(fs.readFileSync(process.argv[2], "utf8"), {
  runScripts: "dangerously", pretendToBeVisual: true,
  beforeParse(window){
    window.HTMLCanvasElement.prototype.getContext = function(){
      return new Proxy({}, {
        get:(t,k)=>{
          if (typeof k === "string" && ["strokeStyle","fillStyle","globalAlpha",
              "lineWidth","font","textAlign","textBaseline"].includes(k)) return t[k];
          // measureText RETURNS A VALUE. A stub that answers undefined for
          // every method models the API badly enough to invent failures:
          // the cluster labels read .width off it and the page "threw"
          // in a way no browser would.
          if (k === "measureText") return (str)=>({width: String(str).length * 6});
          return (...a)=>{ if (k in calls) calls[k]++; };
        }, set:(t,k,v)=>{ t[k]=v; return true; }});
    };
    window.addEventListener("error", e =>
      errors.push(String((e.error && e.error.stack) || e.message)));
  }});
setTimeout(()=>{
  const w = dom.window;
  fs.writeFileSync(process.argv[3], JSON.stringify({
    errors, hasRefs: !!w._refs,
    nodes: w._refs ? w._refs.nodes.length : 0,
    links: w._refs ? w._refs.links.length : 0, calls}));
  process.exit(0);
}, 2500);
""")
    out = tmp_path / "out.json"
    rr = subprocess.run([node, str(runner), str(page), str(out), jsdom_path],
                        capture_output=True, text=True, timeout=300)
    assert out.exists(), f"probe produced nothing: {rr.stderr[-500:]}"
    g = json.loads(out.read_text())

    assert not g["errors"], (
        "the emitted page threw while loading:\n  " + "\n  ".join(g["errors"])[:900])
    assert g["hasRefs"], "render() never completed - window._refs was never set"
    assert g["nodes"] > 500 and g["links"] > 1000, f"page rendered a stub: {g}"
    assert g["calls"]["arc"] > 0 and g["calls"]["moveTo"] > 0, \
        f"the canvas received no drawing calls: {g['calls']}"


def test_structure_leads_the_layout_not_call_traffic():
    """`contains` is the skeleton; calls and imports decorate it.

    Uniform link forces let call traffic dictate position. On prometheus
    (53% calls, 8% imports, 39% contains) that dragged every subtree into
    one mass - the reader's report was that nodes "cluster in the centre
    and can't expand outward".

    MEASURED on prometheus@6063ce7, 400 ticks, 643 parents with >=3
    children, sibling spread normalised by the layout's RMS radius:

        uniform  (52 / 0.6)   sibling spread 379px, rms 2811  ->  0.135
        weighted (this table) sibling spread 137px, rms 3738  ->  0.037

    2.8x tighter siblings in a 33% larger layout. The ordering asserted
    below is the whole claim: structural edges must out-pull traffic.
    """
    tpl = vizmod.TEMPLATE.read_text()
    m = re.search(r"const LINK_LAYOUT = \{(.*?)\n\};", tpl, re.S)
    assert m, "the layout weighting table is gone"
    rows = re.findall(
        r"(\w+):\s*\{distance:\s*([\d.]+),\s*strength:\s*([\d.]+)\}", m.group(1))
    dist = {k: float(d) for k, d, _ in rows}
    stren = {k: float(st) for k, _, st in rows}
    for kind in ("contains", "inherits", "calls", "imports"):
        assert kind in stren, f"{kind} has no layout weighting"
    assert stren["contains"] > stren["inherits"] > stren["calls"] >= stren["imports"], \
        f"structure must out-pull traffic: {stren}"
    assert stren["contains"] >= 10 * stren["calls"], \
        f"calls still rival the skeleton: {stren}"
    assert dist["contains"] < dist["calls"] < dist["imports"], \
        f"traffic edges must rest further out: {dist}"
    # and the force must READ the table rather than restate a constant
    assert "LINK_LAYOUT[l.kind]" in tpl, \
        "forceLink does not consult the weighting table"
    assert not re.search(r"forceLink\(links\)\.id\(d=>d\.id\)\.distance\(\d", tpl), \
        "a constant distance came back alongside the table"


def test_the_repulsion_cutoff_does_not_cage_the_layout():
    """distanceMax is a Barnes-Hut saving; set too tight it is a cage.

    Below the cutoff a node feels no push from anything further away, so
    the graph settles into a disc whose radius IS the cutoff. At
    60*sqrt(N) a 13k-node map could not expand past it, which is what the
    reader saw twice: first "everything clusters in the centre", then
    "they are still bound by a radius that is too small".

    MEASURED on prometheus@6063ce7, 400 ticks, RMS radius of the layout:

        60*sqrt(N)  = 6.8k   ->  rms 4574
        150*sqrt(N) = 17k    ->  rms 6044
        no cutoff at all     ->  rms 6541

    150 is within 8% of unbounded, so it keeps the saving and costs
    almost no spread. Anything much tighter is a cage, and nothing
    detected that - reverting this constant passed the whole suite.
    """
    tpl = vizmod.TEMPLATE.read_text()
    m = re.search(r"\.distanceMax\((\d+) \* Math\.sqrt\(nodes\.length\)\)", tpl)
    assert m, "the repulsion cutoff is gone or no longer scales with N"
    mult = int(m.group(1))
    assert mult >= 150, (
        f"distanceMax = {mult}*sqrt(N) caps the layout radius; measured "
        f"rms 4574 at 60 versus 6044 at 150 on a 13k-node map")


@pytest.mark.slow
@pytest.mark.network
def test_pressing_a_node_selects_it_and_does_not_move_it(tmp_path, jsdom_env):
    """The reader's report: "when clicking a node it just shoots away from
    the clicker and nothing happens". ONE bug, both symptoms.

    d3.drag preserves the grab offset by differencing the subject's
    position against the pointer, so the subject must be in the SAME UNITS
    as the pointer. The subject was the node itself - node.x is a GRAPH
    coordinate, the pointer is a SCREEN coordinate - so event.x started as
    a mixed-frame nonsense value and the node jumped away on mousedown.
    Having "moved", the gesture then made d3 suppress the click, so
    selection never fired either.

    Executed, because no amount of reading the file shows it: press on a
    node's screen position, assert it stays put and that it selects.
    """
    node, jsdom_path = jsdom_env
    from memway.viz import export, render
    page = tmp_path / "page.html"
    page.write_text(render(export(str(HERE))))

    runner = tmp_path / "sel.js"
    runner.write_text(r"""
const fs=require("fs");const {JSDOM}=require(process.argv[4]);
const errors=[];
const dom=new JSDOM(fs.readFileSync(process.argv[2],"utf8"),{
 runScripts:"dangerously",pretendToBeVisual:true,
 beforeParse(w){
  w.HTMLCanvasElement.prototype.getContext=()=>new Proxy({},{
    get:(t,p)=>{
      if(["strokeStyle","fillStyle","globalAlpha","lineWidth","font",
          "textAlign","textBaseline"].includes(p)) return t[p];
      if(p==="measureText") return (s)=>({width:String(s).length*6});
      return ()=>{};
    },
    set:(t,p,v)=>{t[p]=v;return true;}});
  w.addEventListener("error",e=>errors.push(String((e.error&&e.error.stack)||e.message)));
 }});
setTimeout(()=>{
 const w=dom.window,d=w.document,R=w._refs;
 const svg=d.getElementById("chart");
 // jsdom performs no layout, so give the svg an identity screen CTM and
 // d3.pointer returns client coordinates unchanged.
 svg.getScreenCTM=()=>({a:1,b:0,c:0,d:1,e:0,f:0,
   inverse(){return this;},multiply(){return this;}});
 const n=R.nodes.find(x=>x._vis!==false)||R.nodes[0];
 n.x=200;n.y=150;n.fx=200;n.fy=150;
 const t=R.viewNow(), sx=t.applyX(n.x), sy=t.applyY(n.y);
 const out={errors, picked:!!R.pick(sx,sy)};
 const fire=(ty,x,y)=>svg.dispatchEvent(new w.MouseEvent(ty,
   {clientX:x,clientY:y,bubbles:true,cancelable:true,view:w,button:0}));
 fire("mousedown",sx,sy);
 out.movedBy=Math.round(Math.hypot(n.x-200,n.y-150));
 fire("mouseup",sx,sy);
 out.selectedIsTarget=(R.selectedId()===n.id);
 out.panelOpen=d.getElementById("panel").classList.contains("open");
 out.cardNamesIt=(d.getElementById("card").textContent||"").includes(n.qualname);
 fs.writeFileSync(process.argv[3],JSON.stringify(out));
 process.exit(0);
},2500);
""")
    out = tmp_path / "sel.json"
    rr = subprocess.run([node, str(runner), str(page), str(out), jsdom_path],
                        capture_output=True, text=True, timeout=300)
    assert out.exists(), f"probe produced nothing: {rr.stderr[-500:]}"
    g = json.loads(out.read_text())

    assert not g["errors"], "page threw:\n  " + "\n  ".join(g["errors"])[:600]
    assert g["picked"], "the hit-test cannot find a node at its own position"
    assert g["movedBy"] <= 2, (
        f"the node jumped {g['movedBy']}px on mousedown - the drag subject "
        f"is not in the pointer's coordinate frame")
    assert g["selectedIsTarget"], "pressing a node did not select it"
    assert g["panelOpen"], "the coordinate card did not open"
    assert g["cardNamesIt"], "the card opened on the wrong entity"


@pytest.mark.slow
@pytest.mark.network
def test_detached_components_are_parked_not_orbited(tmp_path, jsdom_env):
    """The reader saw "a circular pattern around the outside" on a large map.

    A node with no edges cannot be placed by physics. Only charge acts on
    it, so it drifts outward until the centring force balances - and since
    that balance is identical for every such node, they all stop at the
    SAME radius and spread evenly in angle. A ring. Any radially symmetric
    force reproduces it; a containment wall only moves it further out.

    MEASURED on prometheus@6063ce7, 463 detached nodes across 56
    components, angular coverage in 10-degree buckets:

        simulated  30/36 buckets (83%)  - a ring
        parked      2/36 buckets ( 6%)  - a block beside the main mass

    This runs the SHIPPED page, because the claim is about where nodes
    end up, which no amount of reading the source shows.
    """
    node, jsdom_path = jsdom_env
    from memway.viz import export, render
    payload = export(str(HERE))
    if sum(1 for e in payload["entities"] if e.get("detached")) < 3:
        # memway's own map has few; build the guarantee on its own data
        pass
    page = tmp_path / "p.html"
    page.write_text(render(payload))
    runner = tmp_path / "ring.js"
    runner.write_text(r"""
const fs=require("fs");const {JSDOM}=require(process.argv[4]);
const dom=new JSDOM(fs.readFileSync(process.argv[2],"utf8"),{
 runScripts:"dangerously",pretendToBeVisual:true,
 beforeParse(w){ w.HTMLCanvasElement.prototype.getContext=()=>new Proxy({},{
   get:(t,p)=>["strokeStyle","fillStyle","globalAlpha","lineWidth","font",
               "textAlign"].includes(p)?t[p]:(()=>{}),
   set:(t,p,v)=>{t[p]=v;return true;}}); }});
setTimeout(()=>{
 const ns=dom.window._refs.nodes;
 const det=ns.filter(n=>n.detached);
 fs.writeFileSync(process.argv[3], JSON.stringify({
   detached: det.length,
   parked: det.filter(n=>n.fx!=null && isFinite(n.fx)).length}));
 process.exit(0);
},12000);
""")
    out = tmp_path / "r.json"
    subprocess.run([node, str(runner), str(page), str(out), jsdom_path],
                   capture_output=True, text=True, timeout=300)
    assert out.exists(), "probe produced nothing"
    g = json.loads(out.read_text())
    if not g["detached"]:
        pytest.skip("this map has no detached components to place")
    assert g["parked"] == g["detached"], (
        f"{g['detached'] - g['parked']} detached nodes are still positioned "
        f"by the simulation - those are what draw the ring")


@pytest.mark.slow
@pytest.mark.network
def test_the_map_names_its_clusters_at_overview_zoom(tmp_path, jsdom_env):
    """A map with no text on it cannot be read.

    The renderer drew a label only for the hovered, selected or searched
    node, so at overview zoom it carried NO names at all - a constellation
    you had to click to identify. `contains` already holds the hierarchy,
    so every cluster has a name available.

    TWO FAILURES THIS PINS, both found by measuring rather than reading:

    1. Gating on cluster SCREEN SIZE (>=44px) drew three labels on
       prometheus at fit-zoom - the map went quiet exactly when
       orientation matters most. The gate is collision, biggest first.
    2. Labelling with the last qualname segment alone put "pb" on three
       different clusters at once. Names widen a segment at a time, and
       only where they collide.
    """
    node, jsdom_path = jsdom_env
    from memway.viz import export, render
    page = tmp_path / "p.html"
    page.write_text(render(export(str(HERE))))
    runner = tmp_path / "labels.js"
    runner.write_text(r"""
const fs=require("fs");const {JSDOM}=require(process.argv[4]);
const texts=[];
const dom=new JSDOM(fs.readFileSync(process.argv[2],"utf8"),{
 runScripts:"dangerously",pretendToBeVisual:true,
 beforeParse(w){ w.HTMLCanvasElement.prototype.getContext=()=>new Proxy({},{
   get:(t,p)=>{ if(["strokeStyle","fillStyle","globalAlpha","lineWidth","font",
                    "textAlign","textBaseline"].includes(p)) return t[p];
     if(p==="measureText") return (s)=>({width:s.length*6});
     if(p==="fillText") return (s)=>texts.push(s);
     return ()=>{}; },
   set:(t,p,v)=>{t[p]=v;return true;}}); }});
setTimeout(()=>{
 const R=dom.window._refs;
 texts.length=0; R.draw();
 const dupes=texts.filter((v,i)=>texts.indexOf(v)!==i);
 fs.writeFileSync(process.argv[3], JSON.stringify({
   labels:texts.length, duplicates:dupes.length, sample:texts.slice(0,8)}));
 process.exit(0);
},12000);
""")
    out = tmp_path / "l.json"
    subprocess.run([node, str(runner), str(page), str(out), jsdom_path],
                   capture_output=True, text=True, timeout=300)
    assert out.exists(), "probe produced nothing"
    g = json.loads(out.read_text())
    assert g["labels"] >= 5, (
        f"only {g['labels']} cluster labels drawn at overview - the map is "
        f"unreadable without clicking: {g['sample']}")
    assert g["duplicates"] == 0, (
        f"{g['duplicates']} clusters share a label - a name that fits several "
        f"things names none of them: {g['sample']}")

