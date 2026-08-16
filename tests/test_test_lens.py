"""The test/source distinction is a LENS. Data does not move.

Complexity, edges and every stored metric are identical before and after;
the same numbers are partitioned two ways for reading. If that ever stops
being true, the split has become a scale adjustment and the numbers stop
meaning what they meant.

ONE RULE, SHARED. `verify.is_test_entity` decides, for the summary and the
map alike, on PATH AND FILENAME only. A function called `test_connection`
in production code is production code; a qualname heuristic would call it
a test and quietly drop it from the hardest list. The rule also matches
`foo_test.go` and `foo.spec.ts`, which live beside source rather than
under tests/, so a path-prefix-only rule would misclassify every Go and
TypeScript repo (see the docstring on is_test_entity for the bug that
taught us).
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

from memway import query
from memway.verify import is_test_entity
from memway.viz import viz


class E:
    def __init__(self, path, qualname="pkg.f", kind="function"):
        self.path, self.qualname, self.kind = path, qualname, kind


# ------------------------------------------------------------- the rule

@pytest.mark.parametrize("path,expected", [
    ("tests/test_m.py", True),
    ("tests/conftest.py", True),          # a fixture is still under tests/
    ("src/pkg/tests/helper.py", True),    # nested test dirs count
    ("polyglot/scorer_test.go", True),    # Go: beside source, not under tests/
    ("web/button.spec.ts", True),         # TS: same
    ("src/MyThingTest.java", True),
    ("memway/query.py", False),
    ("memway/latest.py", False),          # 'latest' contains 'test'
    ("contest/scoring.py", False),        # so does 'contest'
])
def test_path_rule(path, expected):
    assert is_test_entity(E(path)) is expected, path


def test_qualname_is_never_consulted():
    """A production function named test_* is production code."""
    assert is_test_entity(E("memway/net.py", "memway.net.test_connection")) is False
    assert is_test_entity(E("tests/x.py", "tests.x.helper")) is True


def _path_heuristics_in(path: Path) -> list:
    """AST-level: every `"test" in <expr>.lower()` style comparison.

    Substring scanning cannot do this job. An early version banned the
    strings `startswith` and `lower` and tripped over `k.startswith("_")`
    and `(e.kind or "").lower()`; an earlier one flagged query.py's own
    comment, which quotes the old heuristic on purpose to record the bug.
    Comments and docstrings are invisible to ast.parse, which is the point.
    """
    import ast
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(o, (ast.In, ast.NotIn)) for o in node.ops):
            continue
        left = node.left
        if not (isinstance(left, ast.Constant) and isinstance(left.value, str)
                and "test" in left.value.lower()):
            continue
        for comp in node.comparators:
            src = ast.dump(comp)
            if "'lower'" in src or "'path'" in src:
                out.append(f"line {node.lineno}: '{left.value}' in ...path/lower()")
    return out


def test_only_one_test_rule_exists():
    """Structural: ONE test/source rule, across every module in the package.

    This test used to scan query.py and viz.py only - the two modules I
    happened to be editing when the rule was promoted. It passed while
    FOUR copies of the crude `"test" in path.lower()` heuristic survived in
    indexer.py, harvest.py (twice) and metrics.py. A guard scoped to where
    the author was looking is not a guard.

    It now walks the whole package and NAMES what it scanned, so a new
    module cannot hide by being absent from a hand-written list.
    """
    import ast
    mods = sorted(p for p in (HERE / "memway").glob("*.py")
                  if p.name not in ("__init__.py", "__main__.py"))
    assert len(mods) >= 15, f"only scanned {len(mods)} modules: {[m.name for m in mods]}"
    offenders = {}
    for m in mods:
        if m.name == "verify.py":
            continue                     # the canonical rule lives here
        hits = _path_heuristics_in(m)
        if hits:
            offenders[m.name] = hits
    assert not offenders, (
        "path-based test heuristics outside verify.is_test_entity:\n  " +
        "\n  ".join(f"{k}: {v}" for k, v in offenders.items()) +
        f"\n(scanned {len(mods)} modules: {', '.join(m.name for m in mods)})")

    homegrown = {}
    for m in mods:
        if m.name == "verify.py":
            continue
        names = [n.name for n in ast.walk(ast.parse(m.read_text()))
                 if isinstance(n, ast.FunctionDef) and "is_test" in n.name]
        if names:
            homegrown[m.name] = names
    assert not homegrown, f"a second detector was defined: {homegrown}"


# ------------------------------------------------------- data untouched

@pytest.fixture(scope="module")
def mapped(tmp_path_factory):
    R = tmp_path_factory.mktemp("lens") / "proj"
    (R / "tests").mkdir(parents=True)
    (R / "polyglot").mkdir(parents=True)
    subprocess.run(["git", "-C", str(R), "init", "-q", "-b", "main"], check=True)
    (R / "m.py").write_text(
        'def alpha(x):\n    """D."""\n    if x > 2:\n        return x + 1\n    return x\n')
    (R / "tests" / "test_m.py").write_text(
        'def test_alpha():\n    if True:\n        assert True\n')
    (R / "polyglot" / "scorer_test.go").write_text(
        "package p\n\nfunc TestThing(t *T) {\n}\n")
    subprocess.run(["git", "-C", str(R), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c",
                    "user.name=T", "commit", "-qm", "s", "--no-gpg-sign"],
                   check=True)
    r = subprocess.run([sys.executable, "-m", "memway.cli", "init", str(R)],
                       capture_output=True, text=True, cwd=str(HERE))
    assert r.returncode == 0, r.stderr[-400:]
    return R


def _fingerprint(repo):
    """Every stored metric and edge, byte for byte."""
    out = {}
    for rel in ("metrics/metrics.json", "index/coordinates.json",
                "index/edges.json", "index/raw_edges.json"):
        p = repo / ".coord" / rel
        if p.exists():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_metrics_are_byte_identical_across_the_lens(mapped):
    """THE assertion. A lens reads; it does not rescale."""
    before = _fingerprint(mapped)
    assert before, "fixture has no metrics to compare"
    query.summary(str(mapped))
    assert _fingerprint(mapped) == before, "summary mutated stored data"


def test_complexity_values_are_not_adjusted_for_tests(mapped):
    """The number attached to a test entity is its real number."""
    import json as _json
    metrics = _json.loads((mapped / ".coord" / "metrics" / "metrics.json").read_text())
    s = query.summary(str(mapped))
    by_name = {h["qualname"]: h["complexity"] for h in s["hardest_overall"]}
    from memway.indexer import Indexer
    ix = Indexer(mapped, mapped / ".coord"); ix.load_existing(write_cache=False)
    for cid, e in ix.entities.items():
        if e.qualname in by_name:
            assert by_name[e.qualname] == metrics[cid]["complexity"], \
                f"{e.qualname} was rescaled"


# ------------------------------------------------------ summary, additive

OLD_KEYS = ("entities", "edges", "languages", "kinds", "hardest", "knowledge")


def test_summary_keeps_every_pre_existing_key(mapped):
    """Consumers depend on these. The manifest lesson: add, never rename."""
    s = query.summary(str(mapped))
    for k in OLD_KEYS:
        assert k in s, f"summary lost {k}"


def test_hardest_still_means_source_only(mapped):
    s = query.summary(str(mapped))
    assert all(h["is_test"] is False for h in s["hardest"]), \
        "the headline list must stay source-only"
    for h in s["hardest"]:
        assert "qualname" in h and "complexity" in h


def test_hardest_overall_includes_tests_and_flags_them(mapped):
    s = query.summary(str(mapped))
    names = {h["qualname"]: h["is_test"] for h in s["hardest_overall"]}
    assert any(v for v in names.values()), \
        f"no test entity surfaced in hardest_overall: {names}"
    for q, is_t in names.items():
        assert is_t == ("test" in q.split(".")[0] or "_test" in q), q


def test_entities_by_origin_partitions_the_whole_map(mapped):
    s = query.summary(str(mapped))
    o = s["entities_by_origin"]
    assert set(o) == {"source", "tests"}
    assert o["source"] + o["tests"] == s["entities"], \
        "the split must account for every entity, exactly once"
    assert o["tests"] > 0 and o["source"] > 0


# ------------------------------------------------------------- the map

def test_viz_payload_carries_is_test(mapped, tmp_path):
    out = tmp_path / "map.html"
    viz(str(mapped), str(out))
    html = out.read_text()
    assert '"is_test"' in html, "the map cannot filter what it was not told"


def test_viz_origin_toggle_markup_is_present(mapped, tmp_path):
    """PRESENCE ONLY. Read the name literally: this proves the toggle, the
    predicate text and the styling rule EXIST in the emitted page. It does
    not prove any of them run.

    It was called `..._is_wired_not_merely_present` and it was green while
    the toggle was completely inert, because normalize() dropped the field
    before the predicate ever saw it. A name that claims more than the body
    delivers is worse than no test: it retires the suspicion that would
    have found the bug. Renamed rather than deleted - as a fast smoke layer
    it is still worth having, just not as the only witness.

    The witness lives in test_the_shipped_javascript_actually_filters.
    """
    out = tmp_path / "map.html"
    viz(str(mapped), str(out))
    html = out.read_text()
    assert 'data-origin="source"' in html and 'data-origin="tests"' in html
    assert "originBoxes" in html, "the toggle is not read by anything"
    assert 'origins.has(' in html, "the filter predicate ignores origin"
    assert "originBoxes.forEach" in html, "no change listener on the toggle"
    assert ".node.is-test circle.core" in html, "no visual distinction"
    assert 'd.is_test===true?" is-test":""' in html, "class never applied"


def test_both_origins_default_to_on(mapped, tmp_path):
    """The map stays honest; the lens is opt-in."""
    out = tmp_path / "map.html"
    viz(str(mapped), str(out))
    html = out.read_text()
    for origin in ("source", "tests"):
        m = re.search(rf'<input type="checkbox" data-origin="{origin}"([^>]*)>', html)
        assert m and "checked" in m.group(1), f"{origin} is not on by default"


def test_a_map_without_the_flag_still_renders_as_source(mapped, tmp_path):
    """Backward compatibility: the bundled sample and hand-made JSON have
    no is_test, and must not vanish when the lens ships."""
    out = tmp_path / "map.html"
    viz(str(mapped), str(out))
    html = out.read_text()
    assert 'd.is_test===true?"tests":"source"' in html, \
        "undefined must fall back to source, not disappear"


def test_console_serves_the_same_flag(mapped):
    from memway.console import build_page
    html = build_page(str(mapped), token="t")
    assert '"is_test"' in html
    assert 'data-origin="tests"' in html


# ============================================================ EXECUTION
#
# Everything above this line reads the emitted bytes. That was not enough:
# 0.53.0 shipped an origin toggle that was correct in the payload, correct
# in the markup, correct in the predicate SOURCE, and completely inert -
# because normalize() rebuilds every node field by field and is_test was
# not on the list. Every presence assertion passed. Unchecking "tests" hid
# nothing; unchecking "source" hid the whole graph.
#
# So the filter is now EXECUTED. Two witnesses:
#
#   1. a Python replica of normalize()'s field list, parsed OUT of the
#      template, so a dropped field fails here with no runtime needed;
#   2. the actual shipped JavaScript, run in node when node is present.
#
# The second is the real witness. The first exists because the suite must
# stay green on a machine with no JS runtime, and a skipped test guards
# nothing.

import json
import re
import shutil
import subprocess

TEMPLATE = HERE / "memway" / "viz_template.html"


def _emitted_payload(repo) -> dict:
    """The JSON the template is actually handed, from a real emitted page."""
    from memway.console import build_page
    html = build_page(str(repo), token="t")
    m = re.search(r'const (?:SAMPLE|DATA)\s*=\s*(\{.*?\});\s*\n', html, re.S)
    assert m, "the emitted page carries no payload"
    return json.loads(m.group(1))


def _normalize_fields() -> set:
    """The field names normalize() actually keeps, parsed from the template."""
    src = TEMPLATE.read_text()
    m = re.search(r"function normalize\(raw\)\{.*?raw\.entities\|\|\[\]\)\.map\(e=>\(\{(.*?)\}\)\)",
                  src, re.S)
    assert m, "normalize()'s field list could not be located"
    return set(re.findall(r"(\w+)\s*:", m.group(1)))


def test_normalize_keeps_is_test(mapped):
    """The exact drop that shipped. A payload key absent from normalize()'s
    field list never reaches the renderer, however correct the payload is.

    MEASURED UNDER SABOTAGE: with `is_test:e.is_test===true` removed from
    normalize(), this test fails, the Python-replica partition test fails,
    and the node execution test fails - while EVERY presence assertion in
    this file stays green, including the one that used to be called
    `..._is_wired_not_merely_present`. That is the record of why presence
    was insufficient, and why it may no longer be the only witness for an
    interactive behaviour.
    """
    assert "is_test" in _normalize_fields(), \
        "normalize() drops is_test; the origin toggle is inert"


def test_the_filter_partitions_the_map_python_replica(mapped):
    """Applies normalize()'s real field list, then the origin predicate."""
    payload = _emitted_payload(mapped)
    fields = _normalize_fields()
    nodes = [{k: e.get(k) for k in fields} for e in payload["entities"]]
    total = len(nodes)
    def vis(checked):
        return sum(1 for n in nodes
                   if ("tests" if n.get("is_test") is True else "source") in checked)
    both, src_only, test_only = vis({"source", "tests"}), vis({"source"}), vis({"tests"})
    assert both == total, "both checked must show everything"
    assert 0 < src_only < total, f"source-only showed {src_only}/{total}"
    assert 0 < test_only < total, f"tests-only showed {test_only}/{total}"
    assert src_only + test_only == total, "the split must be a partition"


@pytest.mark.skipif(shutil.which("node") is None, reason="no JS runtime")
def test_the_shipped_javascript_actually_filters(mapped, tmp_path):
    """THE witness: runs the template's own normalize() and predicate.

    Lifts both verbatim out of the shipped template and executes them over
    a real emitted payload. This is the only test in the file that would
    have failed on 0.53.0 without being told what to look for.
    """
    payload = _emitted_payload(mapped)
    (tmp_path / "p.json").write_text(json.dumps(payload))
    probe = tmp_path / "probe.js"
    probe.write_text(r"""
const fs = require("fs");
const tpl = fs.readFileSync(process.argv[2], "utf8");
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const nm = tpl.match(/function normalize\(raw\)\{[\s\S]*?\n\}/);
if (!nm) { console.error("normalize() not found"); process.exit(2); }
eval(nm[0]);
const pm = tpl.match(/const visible=(d=>[^\n;]+);/);
if (!pm) { console.error("visible predicate not found"); process.exit(2); }
const data = normalize(payload);
function count(checkedOrigins) {
  const kinds = new Set(data.entities.map(e => e.kind));
  const origins = new Set(checkedOrigins);
  const minCx = 0, kOnly = false;
  const visible = eval("(" + pm[1] + ")");
  return data.entities.filter(visible).length;
}
console.log(JSON.stringify({total: data.entities.length,
  both: count(["source","tests"]), source_only: count(["source"]),
  tests_only: count(["tests"])}));
""")
    r = subprocess.run(["node", str(probe), str(TEMPLATE), str(tmp_path / "p.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]
    got = json.loads(r.stdout)
    assert got["both"] == got["total"], got
    assert 0 < got["source_only"] < got["total"], \
        f"unchecking tests hid nothing: {got}"
    assert 0 < got["tests_only"] < got["total"], \
        f"unchecking source hid everything: {got}"
    assert got["source_only"] + got["tests_only"] == got["total"], got


@pytest.mark.skipif(shutil.which("node") is None, reason="no JS runtime")
def test_the_console_page_filters_too(mapped, tmp_path):
    """The console serves the same template; the bug was reported there."""
    from memway.console import build_page
    html = build_page(str(mapped), token="t")
    assert "is_test:e.is_test===true" in html, \
        "the served console page carries a normalize() that drops is_test"


# ==================================================== THE RING RULE
#
# Entries are append-only and never deleted, so a coordinate accumulates
# its own history. The ring was `knowledge.some(k => k.stale)`, which meant
# a coordinate that went coral once could NEVER return to amber, however
# many times a human re-read the code and wrote a fresh confirm. Meanwhile
# `attention` used a different rule - suppress when ANY confirm is fresh -
# so the map and the queue disagreed about the same coordinate.
#
# Per channel, the NEWEST entry decides. Verified by executing the shipped
# helper, not by grepping for it.

from memway.viz import has_unsuperseded_stale


def test_a_fresh_entry_supersedes_a_stale_one_in_its_channel():
    """The regression: re-reading the code must be able to clear the ring."""
    assert has_unsuperseded_stale([
        {"channel": "confirm", "stale": True},      # older
        {"channel": "confirm", "stale": False},     # newer, supersedes
    ]) is False


def test_a_stale_entry_with_nothing_newer_still_shows():
    """The mirror. Without it, 'never coral' would pass the test above."""
    assert has_unsuperseded_stale([{"channel": "confirm", "stale": True}]) is True


def test_channels_are_judged_separately():
    """A fresh confirm does not vouch for a stale note.

    ORDER MATTERS IN THIS FIXTURE, and the first version got it wrong: with
    the stale note LAST, collapsing every channel into one bucket still
    yields True, so the test passed against a sabotage that merged them.
    The stale note goes FIRST, followed by a newer entry in a different
    channel - the only arrangement where merging gives the wrong answer.
    """
    assert has_unsuperseded_stale([
        {"channel": "notes", "stale": True},        # unanswered
        {"channel": "confirm", "stale": False},     # newer, DIFFERENT channel
    ]) is True
    # and the same two in one channel really is superseded
    assert has_unsuperseded_stale([
        {"channel": "notes", "stale": True},
        {"channel": "notes", "stale": False},
    ]) is False


def test_no_knowledge_is_not_stale():
    assert has_unsuperseded_stale([]) is False


def test_the_payload_carries_the_flag(mapped, tmp_path):
    out = tmp_path / "m.html"
    viz(str(mapped), str(out))
    assert '"knowledge_stale"' in out.read_text()


def test_the_template_has_ONE_ring_rule():
    """Two inline copies is how the behind-count shipped without the
    exclusion the dirty check already had."""
    tpl = (HERE / "memway" / "viz_template.html").read_text()
    assert "function ringStale(d)" in tpl
    assert tpl.count("ringStale(d)") >= 3, "both ring sites must call the helper"
    assert 'stamp-ring"+(d.knowledge.some(k=>k.stale)' not in tpl, \
        "an inline copy of the ring rule came back"


@pytest.mark.skipif(shutil.which("node") is None, reason="no JS runtime")
def test_the_shipped_ring_rule_clears_on_re_confirmation(mapped, tmp_path):
    """Executes normalize() and ringStale() from the emitted page against a
    payload where a stale entry has been superseded."""
    out = tmp_path / "m.html"
    viz(str(mapped), str(out))
    payload = {"repo": "t", "edges": [], "entities": [
        {"id": "C-1", "qualname": "a", "kind": "function", "file": "a.py",
         "lines": "1-2", "complexity": 1, "knowledge_stale": False,
         "knowledge": [{"channel": "confirm", "stale": True, "text": "old"},
                       {"channel": "confirm", "stale": False, "text": "new"}]},
        {"id": "C-2", "qualname": "b", "kind": "function", "file": "b.py",
         "lines": "1-2", "complexity": 1, "knowledge_stale": True,
         "knowledge": [{"channel": "confirm", "stale": True, "text": "old"}]}]}
    (tmp_path / "p.json").write_text(json.dumps(payload))
    probe = tmp_path / "r.js"
    probe.write_text(r"""
const fs=require("fs");
const tpl=fs.readFileSync(process.argv[2],"utf8");
const p=JSON.parse(fs.readFileSync(process.argv[3],"utf8"));
eval(tpl.match(/function normalize\(raw\)\{[\s\S]*?\n\}/)[0]);
eval(tpl.match(/function ringStale\(d\)\{[\s\S]*?\n\}/)[0]);
const d=normalize(p);
console.log(JSON.stringify(d.entities.map(e=>[e.qualname, ringStale(e)])));
""")
    r = subprocess.run(["node", str(probe), str(out), str(tmp_path / "p.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-300:]
    got = dict(json.loads(r.stdout))
    assert got["a"] is False, "a re-confirmed coordinate stayed coral"
    assert got["b"] is True, "an unanswered stale entry lost its ring"
