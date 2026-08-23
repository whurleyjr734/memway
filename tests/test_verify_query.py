"""`--json verify-change`: the seventh query.

It answers "given the working tree against the saved map, what changed and
what guards it". Changed entities, the impacted radius, and the tests that
reach the change THROUGH THE EDGE GRAPH.

IT REPORTS, IT DOES NOT RUN. Selecting tests is a read; executing them is
not. `run` is pinned False on this surface and takes no argument, so a
query can never shell out to pytest. A tool that runs your test suite is a
different tool than one that tells you which tests matter.

IT IS THE ONE QUERY THAT WRITES. Every other entry in QUERIES leaves
.coord byte-identical (see test_read_fence). This one re-indexes and
rewrites the edge cache so the map reflects the tree it just measured -
long-standing MCP behaviour, shared deliberately rather than forked. That
asymmetry is asserted here so nobody infers inertness from the company it
keeps, and so a future change to it is a decision rather than an accident.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import query


SRC = '''"""Module m."""


def clamp(x, lo, hi):
    """Clamp."""
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def caller(x):
    return clamp(x, 0, 10)
'''

TESTS = '''from m import clamp


def test_clamp_bounds():
    assert clamp(5, 0, 10) == 5


def test_unrelated():
    assert True
'''


@pytest.fixture
def repo(tmp_path):
    R = tmp_path / "proj"
    (R / "tests").mkdir(parents=True)
    subprocess.run(["git", "-C", str(R), "init", "-q", "-b", "main"], check=True)
    (R / "m.py").write_text(SRC)
    (R / "tests" / "test_m.py").write_text(TESTS)
    subprocess.run(["git", "-C", str(R), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t", "-c",
                    "user.name=T", "commit", "-qm", "s", "--no-gpg-sign"],
                   check=True)
    r = subprocess.run([sys.executable, "-m", "memway.cli", "init", str(R)],
                       capture_output=True, text=True, cwd=str(HERE))
    assert r.returncode == 0, r.stderr[-400:]
    return R


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "memway.cli", *args],
                          capture_output=True, text=True, cwd=str(HERE))


# ------------------------------------------------------------- plumbing

def test_verify_change_is_a_registered_query():
    assert "verify-change" in query.QUERIES
    # Named, not just counted: a bare count tells you something moved but
    # not what, and it passes when one query is swapped for another.
    assert set(query.QUERIES) == {
        "summary", "at", "show", "before-edit", "lineage", "dig",
        "verify-change", "attention",
        "search",
        "review",
        # graph primitives (0.63.0) - answerable on a map with no
        # knowledge in it, which is what the memory queries cannot do
        "clones", "tests-for",
    }, sorted(query.QUERIES)


def test_the_query_and_the_mcp_tool_share_one_implementation():
    """The stamp_for pattern: one rule, one place. Two answers to 'what did
    I just break' is worse than none."""
    import ast
    tree = ast.parse((HERE / "memway" / "query.py").read_text())
    defs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert defs.count("verify_change") == 1, "verify_change was reimplemented"
    mcp = (HERE / "memway" / "mcp.py").read_text()
    assert "query.verify_change(" in mcp, "MCP no longer calls the shared entry"
    assert "from .verify import verify_change" not in mcp, \
        "MCP bypasses the shared entry"


def test_the_query_never_executes_tests():
    """`run` is pinned False and unreachable from the CLI surface."""
    import inspect
    src = inspect.getsource(query)
    entry = src[src.index('"verify-change": lambda'):]
    entry = entry[:entry.index("\n")]
    assert "run" not in entry, f"the query surface exposes run: {entry}"
    assert query.QUERIES["verify-change"].__code__.co_argcount == 2


# --------------------------------------------------------- the answer

def test_a_real_change_is_detected_with_its_covering_tests(repo):
    """The whole contract, against an actual working-tree edit."""
    (repo / "m.py").write_text(SRC.replace("return lo", "return lo + 0"))
    out = query.QUERIES["verify-change"](str(repo), [])

    changed = {c if isinstance(c, str) else c.get("qualname", "")
               for c in out["changed"]}
    assert any("clamp" in c for c in changed), f"clamp not seen as changed: {changed}"
    assert out["impacted"] >= 1, "the caller is not in the impacted radius"

    tests = out["tests"]
    assert set(tests) >= {"grounded", "name_hit"}, tests
    reached = " ".join(tests["grounded"]) + " ".join(tests["name_hit"])
    assert "test_m.py" in reached, f"no covering test found: {tests}"


def test_covering_tests_are_found_through_edges_not_names(repo):
    """`grounded` means the graph reached it. The tiering is the point:
    a name match is labelled a guess, not passed off as coverage."""
    (repo / "m.py").write_text(SRC.replace("return hi", "return hi - 0"))
    out = query.QUERIES["verify-change"](str(repo), [])
    assert isinstance(out["tests"]["grounded"], list)
    assert isinstance(out["tests"]["name_hit"], list)
    for node in out["tests"]["grounded"]:
        assert "::" in node or node.endswith(".py"), node


def test_only_test_entities_are_offered_as_coverage(repo):
    """is_test_entity - the rule promoted in the lens commit - decides.

    BOTH halves are asserted, and the second is the one with teeth. "no
    source function appears" alone is unfalsifiable here: removing the
    is_test guards does not pollute the output, it empties it, so an
    absence-only assertion passes just as happily on a broken selector as
    on a working one. Requiring real coverage to be found is what makes
    this test able to fail.
    """
    (repo / "m.py").write_text(SRC.replace("return lo", "return lo + 0"))
    out = query.QUERIES["verify-change"](str(repo), [])
    grounded = out["tests"]["grounded"]
    assert grounded, "no covering test found at all - selection is broken"
    everything = " ".join(grounded + out["tests"]["name_hit"])
    assert "caller" not in everything, "a source function was offered as a test"
    for node in grounded:
        f = node.split("::")[0]
        assert "test" in Path(f).name, f"{f} is not a test file"


def test_no_change_gives_a_clean_empty_shape(repo):
    """Same keys, empty values. A caller must not have to special-case it."""
    out = query.QUERIES["verify-change"](str(repo), [])
    assert out["changed"] == []
    assert out["impacted"] == 0
    assert out["tests"] == {"grounded": [], "name_hit": []}


# ------------------------------------------------------------- the CLI

def test_cli_json_verify_change_returns_parseable_json(repo):
    (repo / "m.py").write_text(SRC.replace("return lo", "return lo + 0"))
    r = _cli("--json", "verify-change", str(repo))
    assert r.returncode == 0, r.stderr[-300:]
    data = json.loads(r.stdout)
    assert "changed" in data and "tests" in data


def test_an_unknown_query_still_lists_the_available_ones(repo):
    r = _cli("--json", "verify_change", str(repo))     # underscore, a typo
    assert r.returncode == 1
    err = json.loads(r.stdout)["error"]
    assert "verify-change" in err, "the error must name the real query"


# ------------------------------------ the write, deliberately reversed

def test_this_query_no_longer_writes(repo):
    """The old test here asserted the OPPOSITE, and said what to do if the
    behaviour ever changed: "if that is intended, move it under the read
    fence in test_read_fence.py rather than leaving this test as the only
    record." 0.54.1 did exactly that, so this now asserts inertness and
    the fence carries the real guarantee.

    Why the reversal: the write was justified as keeping the map in step
    with the tree it just measured. After 0.54.0 a re-index can perform
    the sketch migration, which announces itself on stdout - invisible to
    a --json caller. A read that can silently migrate a map is worse than
    a map one commit behind, and the lag warning already covers the
    latter.
    """
    import hashlib
    def fp():
        return {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((repo / ".coord").rglob("*"))
                if p.is_file() and "log" not in p.parts}
    (repo / "m.py").write_text(SRC.replace("return lo", "return lo + 0"))
    before = fp()
    out = query.QUERIES["verify-change"](str(repo), [])
    after = fp()
    assert out["changed"], "fixture no longer produces a change to measure"
    assert after == before, (
        f"verify-change wrote: "
        f"{sorted(k for k in set(before)|set(after) if before.get(k)!=after.get(k))}")
