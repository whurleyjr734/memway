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


def test_only_one_test_rule_exists():
    """Structural: aggregate views join the shared rule, never define one.

    Checked by AST, not substring. A first attempt banned the strings
    `startswith` and `lower` outright and failed on `k.startswith("_")` and
    `(e.kind or "").lower()`, which have nothing to do with tests; an
    earlier attempt flagged query.py's own comment, which quotes the old
    heuristic on purpose to record the bug. The invariant is narrow: use
    the shared rule, and do not grow a second detector.
    """
    import ast
    for mod in ("query.py", "viz.py"):
        src = (HERE / "memway" / mod).read_text()
        tree = ast.parse(src)
        assert "is_test_entity" in src, f"{mod} does not use the shared rule"
        homegrown = [n.name for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and "test" in n.name.lower()]
        assert not homegrown, f"{mod} defines its own test detector: {homegrown}"


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


def test_viz_origin_toggle_is_wired_not_merely_present(mapped, tmp_path):
    """Sabotaging the filter must FAIL this, not blank the page."""
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
