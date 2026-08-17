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
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

pytestmark = [pytest.mark.slow, pytest.mark.network]

# Pinned shas: a floor measured against a moving target is not a floor.
CORPUS = [
    # repo url, name, sha, min overall recall %, max missing local edges
    ("https://github.com/psf/requests", "requests", "8068356", 95, 3),
    ("https://github.com/pallets/click", "click", "cbd7a41", 66, 4),
    ("https://github.com/Textualize/rich", "rich", "9d8f9a3", 96, 2),
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
            self.out.append((self.stack[-1], nm, isinstance(f, ast.Attribute)))
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
    ents, calls = _load(repo)
    short = {}
    for e in ents:
        if e.get("kind") in ("function", "method"):
            short.setdefault(e["qualname"].split(".")[-1], []).append(
                e["qualname"])
    # Unambiguous targets only. A name with two definitions cannot be
    # scored against a resolver whose whole job is refusing to guess.
    uniq = {k: v[0] for k, v in short.items()
            if len(v) == 1 and len(k) >= 5 and not k.startswith("_")}
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
        for src, tgt, _via in v.out:
            if tgt not in uniq or src not in short or src == tgt:
                continue
            checked += 1
            if any(s.split(".")[-1] == src and d == uniq[tgt]
                   for s, d in calls):
                hit += 1
            else:
                parts = uniq[tgt].split(".")
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
