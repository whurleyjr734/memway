"""Graph primitives: answers that do not depend on accumulated knowledge.

Every memory tool returns nothing on a map indexed five minutes ago. These
must return something, because that is the entire argument for their
existence - the cost of the workflow is immediate and the benefit of the
memory was deferred, which is the shape that gets a tool uninstalled.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway.primitives import clones, covering_tests, MIN_CLONE_LOC


def _git(r, *a):
    return subprocess.run(["git", "-C", str(r), *a],
                          capture_output=True, text=True)


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "memway.cli",
                           *[str(a) for a in args]],
                          capture_output=True, text=True, cwd=str(HERE))


BODY = ("def {name}(a, b):\n"
        "    total = a + b\n"
        "    scaled = total * 2\n"
        "    return scaled - 1\n")


@pytest.fixture
def repo(tmp_path):
    """Two identical bodies under DIFFERENT names, plus a decoy.

    The names differing is the whole point: it is what makes this
    invisible to grep and visible to a name-insensitive structure hash.
    """
    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text(BODY.format(name="alpha")
                            + "\n\n" + BODY.format(name="beta")
                            + "\n\ndef unrelated(x):\n"
                              "    if x:\n"
                              "        return x * 7\n"
                              "    return 0\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "one", "--no-gpg-sign")
    assert _cli("init", r).returncode == 0
    return r


def test_clones_finds_a_copy_under_a_different_name(repo):
    """THE reason this passes the grep filter. `alpha` and `beta` have
    identical bodies and different names; no text search finds that pair,
    because the only shared text is the body and the names differ."""
    r = clones(str(repo), "m.alpha")
    assert "error" not in r, r
    names = {m["qualname"] for m in r["identical"]}
    assert names == {"m.beta"}, r
    assert "m.unrelated" not in names


def test_clones_repo_wide_groups_and_excludes_nothing_silently(repo):
    r = clones(str(repo))
    assert r["groups_total"] == 1, r
    g = r["groups"][0]
    assert g["count"] == 2
    assert {m["qualname"] for m in g["members"]} == {"m.alpha", "m.beta"}
    # NO SILENT SAMPLING: the floor is an exclusion and its size is stated.
    assert "excluded_below_min_loc" in r and "min_loc" in r
    assert r["min_loc"] == MIN_CLONE_LOC


def test_the_clone_floor_is_reported_and_actually_excludes(tmp_path):
    """A structure hash on a one-liner is noise, so there is a floor - and
    a floor nobody can see is indistinguishable from a bug."""
    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text(
        "def a():\n    return 1\n\n\ndef b():\n    return 1\n")
    _git(r, "init", "-q", "-b", "main")
    assert _cli("init", r).returncode == 0

    high = clones(str(r), min_loc=99)
    assert high["groups_total"] == 0, high
    assert high["excluded_below_min_loc"] >= 2, (
        f"the floor excluded entities without saying so: {high}")
    low = clones(str(r), min_loc=1)
    assert low["groups_total"] == 1, (
        f"[fixture] the two bodies are not structurally equal: {low}")


def test_near_tier_is_a_score_and_is_off_by_default(repo):
    """The identical tier is a hash match - certain. The near tier is a
    minhash ESTIMATE and must arrive carrying its number, never as a
    verdict, and never unasked."""
    assert "near" not in clones(str(repo), "m.alpha")
    r = clones(str(repo), "m.alpha", near=0.1)
    assert "near" in r and r["near_threshold"] == 0.1
    for m in r["near"]:
        assert 0.0 <= m["similarity"] <= 1.0, m
        assert m["qualname"] != "m.beta", (
            "an identical body leaked into the near tier; the tiers must "
            "not double-count")


def test_near_rejects_an_out_of_range_threshold(repo):
    assert "error" in clones(str(repo), "m.alpha", near=4.0)


def test_tests_for_separates_evidence_from_a_name_match(tmp_path):
    """The two tiers answer different questions and merging them would
    give a name match the authority of a traced path."""
    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text("def widget(x):\n    return x + 1\n")
    (r / "test_m.py").write_text(
        "from m import widget\n\n\n"
        "def test_widget_adds_one():\n"
        "    assert widget(1) == 2\n")
    # a test file that only MENTIONS the name, with no import or call
    (r / "test_mentions.py").write_text(
        "def test_unrelated():\n"
        "    # widget is discussed here but never called\n"
        "    assert True\n")
    _git(r, "init", "-q", "-b", "main")
    assert _cli("init", r).returncode == 0

    d = covering_tests(str(r), "m.widget")
    assert "error" not in d, d
    assert any("test_widget_adds_one" in g for g in d["grounded"]), d
    assert not any("test_mentions" in g for g in d["grounded"]), (
        f"a file that merely mentions the name was reported as grounded: "
        f"{d['grounded']}")
    assert any("test_mentions" in n for n in d["name_hit"]), (
        f"the name-only file was dropped instead of being labelled a "
        f"guess: {d}")


def test_tests_for_uses_the_same_lens_as_verify_change():
    """One implementation. The walk and its two tiers lived inline in
    verify_change and were about to be written a second time - the shape
    every drifted rule in this project started with."""
    import ast
    src = (HERE / "memway" / "primitives.py").read_text()
    tree = ast.parse(src)
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "covering_tests"][0]
    body = ast.dump(fn)
    assert "tests_reaching" in body, "covering_tests no longer delegates the lens"
    for banned in ("deque", "is_test_entity", "_pytest_node"):
        assert banned not in body, (
            f"covering_tests reimplements the lens ({banned} appears in its "
            f"body) - there must be exactly one walk")


def test_an_unknown_ref_is_an_actionable_error(repo):
    for r in (clones(str(repo), "nope_not_here"),
              covering_tests(str(repo), "nope_not_here")):
        assert "error" in r, r
        assert "closest" in r or "hint" in r, (
            f"an unresolved ref must say what to try next: {r}")


def test_all_three_doors_agree(repo):
    """CLI, --json and MCP are the same function or they will drift."""
    from memway import mcp
    direct = clones(str(repo), "m.alpha")
    j = json.loads(_cli("--json", "clones", repo, "m.alpha").stdout)
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "memway_clones",
                               "arguments": {"ref": "m.alpha"}}}, str(repo))
    m = json.loads(r["result"]["content"][0]["text"])
    assert direct["identical"] == j["identical"] == m["identical"], (
        direct, j, m)
    cli_text = _cli("clones", repo, "m.alpha")
    assert cli_text.returncode == 0, cli_text.stderr
    assert "m.beta" in cli_text.stdout, cli_text.stdout


def test_clone_groups_are_bounded_at_BOTH_levels(tmp_path):
    """THE RULE APPLIED ONCE AND FORGOTTEN ONE LEVEL DOWN.

    The first version capped GROUPS at twelve and let each carry every
    member. On pytest's map that returned 12 groups holding 193 member
    rows - 36,433 characters, ~9k tokens - and nothing said a list had
    been cut, because none had. payload.py's own docstring names this
    shape: "a list that quietly stops at twelve IS a sampled list". This
    one did not stop at all.
    """
    from memway.primitives import MEMBERS_CAP
    r = tmp_path / "p"
    r.mkdir()
    body = ("def {name}(a, b):\n"
            "    total = a + b\n"
            "    scaled = total * 2\n"
            "    return scaled - 1\n")
    n = MEMBERS_CAP + 4
    (r / "m.py").write_text(
        "\n\n".join(body.format(name=f"f{i}") for i in range(n)))
    _git(r, "init", "-q", "-b", "main")
    assert _cli("init", r).returncode == 0

    d = clones(str(r))
    assert d["groups_total"] == 1, d
    g = d["groups"][0]
    assert g["count"] == n, (
        f"[fixture] the {n} bodies are not one group: {g}")
    assert g["members_shown"] == MEMBERS_CAP, g
    assert g["members_total"] == n, g
    assert len(g["members"]) == MEMBERS_CAP, (
        f"the member list was not actually bounded: {len(g['members'])}")
    # AND THE COUNT SURVIVES THE CUT - it is the actionable part.
    assert g["count"] > g["members_shown"], (
        "a reader must still learn how many copies exist")


BODY_A = ("def {name}(a, b):\n"
          "    total = a + b\n"
          "    scaled = total * 2\n"
          "    return scaled - 1\n")
BODY_B = ("def {name}(x):\n"
          "    if x is None:\n"
          "        return []\n"
          "    return sorted(set(x))\n")


@pytest.fixture
def split_repo(tmp_path):
    """A SMALL production group and a BIGGER test group.

    The sizes are deliberate: ranking by count alone puts the test group
    first, so a test that used equal sizes would pass whether or not
    origin is consulted.
    """
    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text(BODY_A.format(name="alpha")
                            + "\n\n" + BODY_A.format(name="beta"))
    (r / "test_m.py").write_text(
        "\n\n".join(BODY_B.format(name=f"test_case_{i}") for i in range(4)))
    _git(r, "init", "-q", "-b", "main")
    assert _cli("init", r).returncode == 0
    return r


def test_production_duplication_outranks_a_bigger_test_group(split_repo):
    """THE POINT. On pydantic, 351 of 471 groups are test-only, so ranking
    by size handed the whole page to parametrized tests and never surfaced
    ten identical property accessors in production networking code."""
    d = clones(str(split_repo))
    assert d["groups_total"] == 2, d
    first, second = d["groups"][0], d["groups"][1]
    assert first["origin"] == "production", d["groups"]
    assert second["origin"] == "test-only", d["groups"]
    assert second["count"] > first["count"], (
        "[fixture] the test group is not bigger, so size-ranking and "
        "origin-ranking agree and this proves nothing")


def test_the_split_is_a_census_and_filters_nothing(split_repo):
    """Not a filter. Excluding tests would discard real duplication
    silently, which is the thing this project refuses to do."""
    d = clones(str(split_repo))
    o = d["groups_by_origin"]
    assert sum(o.values()) == d["groups_total"], (o, d["groups_total"])
    assert o == {"production": 1, "mixed": 0, "test-only": 1}, o
    origins = {g["origin"] for g in d["groups"]}
    assert "test-only" in origins, (
        f"a test group was dropped rather than ranked below: {d['groups']}")


def test_mixed_is_its_own_bucket(tmp_path):
    """A body in BOTH production and a test is a different finding from
    duplication inside either - sometimes a test reimplementing logic
    instead of calling it."""
    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text(BODY_A.format(name="alpha"))
    (r / "test_m.py").write_text(BODY_A.format(name="test_alpha_copy"))
    _git(r, "init", "-q", "-b", "main")
    assert _cli("init", r).returncode == 0
    d = clones(str(r))
    assert d["groups_total"] == 1, d
    assert d["groups"][0]["origin"] == "mixed", d["groups"][0]
    assert d["groups_by_origin"]["mixed"] == 1, d["groups_by_origin"]


def test_origin_uses_the_one_test_rule():
    """is_test_entity is THE test/source rule - summary, viz and the test
    lens all join there. A second copy is how two views come to disagree
    about the same repo."""
    import ast
    src = (HERE / "memway" / "primitives.py").read_text()
    fn = [n for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.FunctionDef) and n.name == "_origin"][0]
    body = ast.dump(fn)
    assert "is_test_entity" in body, "_origin no longer delegates the rule"
    for banned in ("tests", "test_", "suffix", "parts"):
        assert f"'{banned}'" not in body and f'"{banned}"' not in body, (
            f"_origin decides what a test is by itself ({banned!r} appears "
            f"in its body)")
