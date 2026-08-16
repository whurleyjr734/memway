"""
verify: the post-change mirror of before_edit.

before_edit answers "what will this change touch?" - verify answers
"what DID my change touch, which tests cover that flow, and do they pass?"

Data flow:
  1. Re-index against the saved index -> changed/added/removed entities
     (the same diff the lineage detector consumes).
  2. Multi-source reverse reachability over the confidence-weighted edge
     graph (calls/imports + closure containment) -> the impacted set.
  3. Test selection, tiered with the same honesty rules as everything
     else in memway:
       grounded  - test entities reached through actual graph edges
       name-hit  - test files that mention a changed entity's name but
                   have no resolved edge (dynamic dispatch, fixtures);
                   selected, but labeled as the guess it is
  4. Optionally execute exactly that selection with pytest.

An agent's loop becomes: before_edit -> edit -> verify_change.
"""
from __future__ import annotations

import re
import subprocess
from collections import deque
from pathlib import Path


_TEST_FILE = {
    ".py":   lambda n: n.startswith("test_") or n.endswith("_test.py"),
    ".go":   lambda n: n.endswith("_test.go"),
    ".js":   lambda n: ".test." in n or ".spec." in n,
    ".jsx":  lambda n: ".test." in n or ".spec." in n,
    ".ts":   lambda n: ".test." in n or ".spec." in n,
    ".tsx":  lambda n: ".test." in n or ".spec." in n,
    ".java": lambda n: n.endswith("Test.java") or n.endswith("Tests.java"),
}

_RUNNER = {".go": "go test", ".js": "node", ".jsx": "node", ".ts": "node",
           ".tsx": "node", ".java": "junit"}


def is_test_entity(e) -> bool:
    """Is this entity part of a test source, in ANY supported language?

    THE one test/source rule. Aggregate views (summary, viz, console) join
    here rather than each deciding for itself; a second rule somewhere else
    is how two views start disagreeing about the same repo.

    PATH AND FILENAME ONLY, never the qualname. A function called
    `test_connection` in production code is production code.

    This was path-only ("tests"/"test" dir, or a test_ prefix), which are
    Python conventions: Go's foo_test.go and TS's foo.spec.ts never
    matched. A change to Go code therefore reported "0 test(s) reached via
    graph edges" - an answer shaped like "nothing covers this" - while the
    graph itself held the covering edge TestClampBounds -> clamp.
    """
    p = Path(e.path)
    if any(part in ("tests", "test", "__tests__") for part in p.parts):
        return True
    pat = _TEST_FILE.get(p.suffix)
    return bool(pat and pat(p.name))


def _is_runnable_test(e, repo_root: Path) -> bool:
    """Would pytest actually COLLECT this entity, not merely contain it?

    is_test_entity is path-based, so it is also true for fixtures and
    module-level helpers that live in tests/. Handing pytest one of those as
    a node id is a usage error (exit 4) and pytest aborts the whole run
    before executing a single test - one bad id silently zeroes out an
    otherwise correct selection. So mirror pytest's own default collection
    rules here rather than trusting the path alone.
    """
    if e.kind not in ("function", "method") or not is_test_entity(e):
        return False
    p = Path(e.path)
    if p.suffix != ".py":
        return False              # pytest is the only runner we shell out to
    if not (p.name.startswith("test_") or p.name.endswith("_test.py")):
        return False                                   # python_files
    parts = _pytest_node(e, repo_root).split("::")[1:]
    if not parts or not parts[-1].startswith("test"):
        return False                                   # python_functions
    return all(p.startswith("Test") for p in parts[:-1])   # python_classes


def _pytest_node(e, repo_root: Path) -> str:
    """entity -> pytest node id (file::Class::method or file::function)."""
    module_parts = Path(e.path).with_suffix("").parts
    qual_parts = e.qualname.split("#")[0].split(".")
    # strip the module-path prefix from the qualname to get the in-file path
    i = 0
    while i < len(qual_parts) and i < len(module_parts) \
            and qual_parts[i] == module_parts[i]:
        i += 1
    infile = qual_parts[i:] if i < len(qual_parts) else qual_parts[-1:]
    return str(Path(e.path)) + ("::" + "::".join(infile) if infile else "")


def verify_change(indexer, edges, repo_root, max_depth: int = 4,
                  run: bool = False, timeout: int = 600) -> dict:
    """Diff the repo against the saved index, trace impact, select tests.

    `indexer` must have load_existing() called; this calls index() itself
    so the returned report reflects the CURRENT working tree.
    """
    repo_root = Path(repo_root)
    report = indexer.index()
    changed_ids = list(report.get("changed", [])) + list(report.get("added", []))
    if not changed_ids:
        return {"changed": [], "impacted": 0, "tests": {"grounded": [],
                "name_hit": []}, "note": "no entity-level changes detected"}

    ents = indexer.entities
    # reverse adjacency over calls/imports, plus closure containment
    rev: dict[str, list[str]] = {}
    for e in edges:
        if e["kind"] in ("calls", "imports"):
            rev.setdefault(e["dst"], []).append(e["src"])
    closure_parent = {}
    for cid, e in ents.items():
        p = ents.get(getattr(e, "parent", None))
        if e.kind == "function" and p and p.kind in ("function", "method"):
            closure_parent[cid] = e.parent

    seen = set(changed_ids)
    q = deque((c, 0) for c in changed_ids)
    while q:
        cid, d = q.popleft()
        if d >= max_depth:
            continue
        nxt = list(rev.get(cid, []))
        p = closure_parent.get(cid)
        if p:
            nxt.append(p)
        for n in nxt:
            if n not in seen:
                seen.add(n)
                q.append((n, d + 1))

    grounded, grounded_files = [], set()
    other_language = []
    for cid in seen:
        e = ents.get(cid)
        if not e or e.kind not in ("function", "method") \
                or not is_test_entity(e):
            continue
        if _is_runnable_test(e, repo_root):
            grounded.append(_pytest_node(e, repo_root))
            grounded_files.add(e.path)
        elif Path(e.path).suffix != ".py":
            # Reached through real edges, but we have no runner for it.
            # Reporting it is the whole point: silently dropping these is
            # how "0 tests reached" comes to mean "no coverage exists"
            # when the graph in fact proved coverage does.
            other_language.append({
                "test": e.qualname, "path": e.path,
                "runner": _RUNNER.get(Path(e.path).suffix, "unknown")})

    # name-hit tier: test files mentioning a changed entity's short name
    # with no resolved edge into the impact set. A guess - and labeled one.
    shorts = {ents[c].qualname.split("#")[0].rsplit(".", 1)[-1]
              for c in changed_ids if c in ents}
    shorts = {s for s in shorts if len(s) > 3}          # skip noise names
    name_hit = set()
    # .py only: name_hit files are handed to pytest verbatim below, and a
    # non-Python path there is the same exit-4 abort that fixture ids caused.
    test_files = {e.path for e in ents.values()
                  if is_test_entity(e) and Path(e.path).suffix == ".py"}
    for tf in test_files:
        if tf in grounded_files:
            continue
        try:
            text = (repo_root / tf).read_text()
        except OSError:
            continue
        if any(re.search(rf"\b{re.escape(s)}\b", text) for s in shorts):
            name_hit.add(tf)

    out = {
        "changed": [ents[c].qualname for c in changed_ids if c in ents],
        "impacted": len(seen) - len(changed_ids),
        "tests": {
            "grounded": sorted(grounded),
            "name_hit": sorted(name_hit),
            "other_language": sorted(other_language,
                                     key=lambda t: t["test"]),
        },
        "note": (f"{len(grounded)} test(s) reached via graph edges; "
                 f"{len(name_hit)} file(s) selected by name only - "
                 f"treat those as guesses"
                 + (f"; {len(other_language)} covering test(s) in other "
                    "languages were reached but CANNOT be run here - see "
                    "tests.other_language" if other_language else "")),
    }

    if run and (grounded or name_hit):
        selection = sorted(grounded) + sorted(name_hit)
        proc = subprocess.run(
            ["python3", "-m", "pytest", "-q", *selection],
            cwd=repo_root, capture_output=True, text=True, timeout=timeout)
        tail = (proc.stdout or proc.stderr).strip().splitlines()
        out["run"] = {"exit": proc.returncode,
                      "summary": tail[-1] if tail else ""}
    return out
