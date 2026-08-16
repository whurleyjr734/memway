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
    import re
    sx = float(re.search(r'forceX\(W/2\)\.strength\(([\d.]+)\)', tpl).group(1))
    assert 0 < sx <= 0.1, f"{sx} would overpower the link layout"


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
