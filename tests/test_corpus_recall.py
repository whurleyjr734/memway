"""Edge-resolution recall against real repositories, at pinned shas.

WHY THIS EXISTS AND WHY IT IS SEPARATE. Fixtures encode what you thought
of; corpora contain what you didn't (CLAUDE.md lesson 5). The closure
bug 0.55.3 fixes was invisible to every hand-built fixture in this suite
for two releases, because none of them had the caller BE the enclosing
function - the one shape the rule got wrong. It took three real
repositories to see it, and 50 of the 60 dropped edges were in one of
them.

So the pin has two layers. tests/test_units_core2.py holds the fast
fixture that guards every run; this file holds the floor that guards a
release. Neither replaces the other: the fixture would have passed on
the day the bug shipped, and this file is too slow and too networked to
run on every save.

GROUND TRUTH IS stdlib ast WITH REAL SCOPE TRACKING, deliberately not
memway's own edge resolution - a second opinion, not a mirror. It walks
the tree, tracks the enclosing def, and records (caller, called-name).

THE FLOORS ARE NOT TARGETS. They sit just under measured values so that
ordinary drift does not fail the build, and a real regression does. Two
numbers are pinned per repo, and the SECOND one is the discriminating
one: overall recall is dominated by attribute calls that memway
deliberately refuses to guess, so it moves slowly. The count of missing
function-local edges is the number the closure bug actually moved -
60 -> 3 across these three repos.

Marked `slow` and `network`. Run with:  pytest -m network
Skipped by default so a normal run stays offline and fast.
"""

import ast
import builtins
import json
import subprocess
import sys
from pathlib import Path

import pytest

from memway import refs
from memway.parsers import _py_literal_recv

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

pytestmark = [pytest.mark.slow, pytest.mark.network]

# Names Python defines itself. Derived from the interpreter, never typed
# out - the same source memway.parsers.PythonParser reads.
_PY_BUILTINS = frozenset(dir(builtins))

# Pinned shas: a floor measured against a moving target is not a floor.
CORPUS = [
    # repo url, name, sha, min overall recall %, max missing local edges
    ("https://github.com/psf/requests", "requests", "8068356", 96, 1),
    ("https://github.com/pallets/click", "click", "cbd7a41", 68, 1),
    ("https://github.com/Textualize/rich", "rich", "9d8f9a3", 96, 1),
]


class _Scoped(ast.NodeVisitor):
    """(enclosing def name, called bare name, was-attribute) per call."""

    def __init__(self):
        self.stack, self.out = [], []

    def _fn(self, n):
        self.stack.append(n.name)
        self.generic_visit(n)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _fn
    visit_ClassDef = _fn

    def visit_Call(self, n):
        f = n.func
        nm = (f.id if isinstance(f, ast.Name)
              else f.attr if isinstance(f, ast.Attribute) else None)
        if nm and self.stack:
            # The third element is "was this an attribute call"; the
            # fourth is "was the receiver a LITERAL", read through the
            # parser's own predicate so the oracle cannot disagree with
            # the resolver about what a literal is.
            self.out.append((self.stack[-1], nm, isinstance(f, ast.Attribute),
                             _py_literal_recv(n)))
        self.generic_visit(n)


def _load(repo: Path):
    c = repo / ".coord" / "index"
    ents = json.loads((c / "coordinates.json").read_text())
    ents = list(ents.values()) if isinstance(ents, dict) else ents
    ed = json.loads((c / "edges.json").read_text())
    ed = ed.get("edges", ed) if isinstance(ed, dict) else ed
    by = {e["coord_id"]: e for e in ents}
    calls = set()
    for e in ed:
        if e.get("kind") != "calls":
            continue
        s = by.get(e.get("src") or e.get("source"))
        t = by.get(e.get("dst") or e.get("target"))
        if s and t:
            calls.add((s["qualname"], t["qualname"]))
    return ents, calls


def _measure(repo: Path):
    # NORMALISED THROUGH refs, because 0.56.0 made disambiguators real:
    # a raw last-segment key reads `iter_content#3` where the ast oracle
    # says `iter_content`, and every such name silently stops matching.
    # Measured: that alone moved requests from 98% to 94% and looked
    # exactly like a regression. The oracle must speak the same language
    # as the map, and refs is where that language is defined.
    ents, calls = _load(repo)
    short = {}
    for e in ents:
        if e.get("kind") in ("function", "method"):
            short.setdefault(refs.short_of(e["qualname"]), []).append(
                e["qualname"])
    # Unambiguous targets only. A name with two definitions cannot be
    # scored against a resolver whose whole job is refusing to guess.
    uniq = {k: v[0] for k, v in short.items()
            if len(v) == 1 and len(k) >= 5 and not k.startswith("_")}
    kind_of = {refs.short_of(e["qualname"]): e.get("kind") for e in ents}
    checked = hit = local_missing = 0
    for f in repo.rglob("*.py"):
        # RELATIVE to the repo, always. Filtering on the absolute path made
        # every file match "test_" when the repo sat under pytest's
        # tmp_path (.../test_edge_recall_floor_requests0/...), so the
        # oracle saw zero call sites and the floor would have passed on no
        # data at all. The checked > 50 assertion below is what caught it.
        rel = str(f.relative_to(repo))
        if any(s in rel for s in ("test", "docs/")):
            continue
        try:
            tree = ast.parse(f.read_text(errors="replace"))
        except (SyntaxError, ValueError):
            continue
        v = _Scoped()
        v.visit(tree)
        for src, tgt, via, lit in v.out:
            if tgt not in uniq or src not in short or src == tgt:
                continue
            # A LITERAL RECEIVER IS THE LANGUAGE'S. `"{}".format(x)` is
            # str.format, so expecting an edge to a repo method named
            # `format` scores the resolver's correct refusal as a miss.
            #
            # SECOND TIME THIS ORACLE HAS SHARED THE RESOLVER'S BLIND
            # SPOT, and the note below about builtins is the first. When
            # the literal rule landed, sklearn's recall "fell" 87% -> 83%
            # and every one of the 206 lost hits had a literal receiver -
            # measured, not assumed, because the same drop could equally
            # have meant real edges were deleted. An oracle only checks
            # you where it disagrees; where it shares an assumption it
            # certifies it.
            if lit:
                continue
            # THE ORACLE MUST NOT SHARE THE TOOL'S BLIND SPOT. A bare
            # `isinstance(x, y)` is the BUILTIN, not sqlalchemy's
            # Options.isinstance - and this oracle happily attributed it
            # to the method, exactly as the resolver used to. They were
            # wrong in the same way, so they agreed, and the agreement
            # read as 76% recall.
            #
            # When 0.57.2 taught the resolver to refuse those, recall
            # "fell" to 44% - 1,702 edges on three names in sqlalchemy
            # alone (isinstance 1,150, getattr 367, tuple 185). Nothing
            # regressed; the oracle simply kept scoring the old error as
            # correct. A second opinion that shares the blind spot is a
            # mirror, not a check.
            if not via and tgt in _PY_BUILTINS \
                    and kind_of.get(tgt) == "method":
                continue
            checked += 1
            if any(refs.short_of(s) == src and d == uniq[tgt]
                   for s, d in calls):
                hit += 1
            else:
                parts = refs.base_of(uniq[tgt]).split(".")
                if len(parts) >= 2 and parts[-2] == src:
                    local_missing += 1          # the closure case
    return checked, hit, local_missing


@pytest.mark.parametrize("url,name,sha,floor,max_local", CORPUS,
                         ids=[c[1] for c in CORPUS])
def test_edge_recall_floor(tmp_path, url, name, sha, floor, max_local):
    repo = tmp_path / name
    # Partial clone: every sha stays reachable (unlike --depth) while the
    # blobs arrive on demand, so pinning a sha costs seconds, not minutes.
    clone = subprocess.run(["git", "clone", "-q", "--filter=blob:none",
                            url, str(repo)], capture_output=True, text=True)
    if clone.returncode != 0:
        pytest.skip(f"clone unavailable: {clone.stderr[-160:]}")
    co = subprocess.run(["git", "-C", str(repo), "checkout", "-q", sha],
                        capture_output=True, text=True)
    if co.returncode != 0:
        pytest.skip(f"sha {sha} unreachable: {co.stderr[-160:]}")

    r = subprocess.run([sys.executable, "-m", "memway.cli", "init", str(repo)],
                       capture_output=True, text=True, cwd=str(HERE))
    assert r.returncode == 0, r.stdout + r.stderr

    checked, hit, local_missing = _measure(repo)
    assert checked > 50, f"oracle found too few call sites to judge: {checked}"
    pct = 100 * hit // checked
    assert pct >= floor, (
        f"{name}@{sha}: edge recall {pct}% is below the {floor}% floor "
        f"({hit}/{checked}). Something stopped resolving.")
    assert local_missing <= max_local, (
        f"{name}@{sha}: {local_missing} closure edges missing (ceiling "
        f"{max_local}). A function calling a helper it defines itself must "
        f"resolve - this is the 0.55.3 regression.")


# ---------------------------------------------------------------------
# AMBIGUITY AT SCALE. The three repos above are small enough that a bare
# name almost always has one definition, so they could never exercise the
# branch where the resolver refuses because SEVERAL match. That gap cost
# two releases: 0.55.5 shipped a guard, 0.56.0 removed it, and 0.57.1
# found the result only by pointing memway at an unfamiliar repository.
# SQLAlchemy is here to be big and ambiguous, not to be representative.
SQLA_URL = "https://github.com/sqlalchemy/sqlalchemy"
SQLA_SHA = "eb5ef2a"


@pytest.fixture(scope="module")
def sqlalchemy_repo(tmp_path_factory):
    """Cloned and indexed ONCE - a cold index here is ~100s."""
    repo = tmp_path_factory.mktemp("sqla") / "sqlalchemy"
    clone = subprocess.run(["git", "clone", "-q", "--filter=blob:none",
                            SQLA_URL, str(repo)], capture_output=True, text=True)
    if clone.returncode != 0:
        pytest.skip(f"clone unavailable: {clone.stderr[-160:]}")
    co = subprocess.run(["git", "-C", str(repo), "checkout", "-q", SQLA_SHA],
                        capture_output=True, text=True)
    if co.returncode != 0:
        pytest.skip(f"sha {SQLA_SHA} unreachable: {co.stderr[-160:]}")
    r = subprocess.run([sys.executable, "-m", "memway.cli", "init", str(repo)],
                       capture_output=True, text=True, cwd=str(HERE))
    assert r.returncode == 0, r.stdout + r.stderr
    return repo


def test_sqlalchemy_recall_floor(sqlalchemy_repo):
    """Measured at eb5ef2a with the BUILTIN-AWARE oracle: 74%
    (1868/2519), 0 closure misses.

    The earlier reading of 76% (3250/4228) was inflated on both sides of
    the comparison. sqlalchemy declares Options.isinstance, Row.tuple and
    BaseRow.getattr as methods, and every bare builtin call resolved to
    them - isinstance 1,150, getattr 367, tuple 185. The resolver counted
    those as hits and the oracle counted them as expectations, so they
    agreed, and the agreement looked like recall.

    0.57.2 refuses them. The denominator falls by 1,709 and the honest
    figure is 74%.
    """
    checked, hit, local_missing = _measure(sqlalchemy_repo)
    assert checked > 1000, f"oracle found too few call sites: {checked}"
    pct = 100 * hit // checked
    assert pct >= 72, (
        f"sqlalchemy@{SQLA_SHA}: edge recall {pct}% is below the 72% floor "
        f"({hit}/{checked}). Measured 76%.")
    assert local_missing <= 1, (
        f"sqlalchemy@{SQLA_SHA}: {local_missing} closure edges missing. "
        f"Measured 0 - the 0.55.3 rule holds at 53k entities.")


def test_ambiguity_at_scale_is_not_reported_as_blindness(sqlalchemy_repo):
    """A refusal is not a gap - the 0.57.1 defect, at the scale that
    produced it.

    THE FIXTURE MUST HAVE THE PROPERTY, checked first and loudly. A
    version of this assertion that quietly passes on a repo where
    `execute` happens to be unique would be exactly the failure this
    release also found in test_cli_units (a guard that became the
    outcome). Measured at eb5ef2a: 41 candidates, old rule 3294, new
    rule 1.
    """
    from memway.query import _ctx, _unresolved_refs_to
    _, _, ix, eb, _ = _ctx(str(sqlalchemy_repo))
    assert len(ix.candidates("execute")) >= 20, (
        "this repo no longer exhibits ambiguity at scale, so the test below "
        "would pass without exercising anything - repin the sha")

    ent = ix.entities[ix.by_qualname[
        "lib.sqlalchemy.orm.session.Session.execute"]]
    n = _unresolved_refs_to(ix, getattr(eb, "edges", []), ent)
    assert n <= 5, (
        f"{n} references to `execute` reported as unresolvable. Ambiguous "
        f"names are being counted as blind spots again: the resolver "
        f"declines to guess between {len(ix.candidates('execute'))} "
        f"candidates, and declining is not a gap. Measured 1.")
