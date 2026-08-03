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
     else in coordsys:
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


def _is_test_entity(e) -> bool:
    p = Path(e.path)
    return any(part in ("tests", "test") for part in p.parts) \
        or p.name.startswith("test_")


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
    for cid in seen:
        e = ents.get(cid)
        if e and e.kind in ("function", "method") and _is_test_entity(e):
            grounded.append(_pytest_node(e, repo_root))
            grounded_files.add(e.path)

    # name-hit tier: test files mentioning a changed entity's short name
    # with no resolved edge into the impact set. A guess - and labeled one.
    shorts = {ents[c].qualname.split("#")[0].rsplit(".", 1)[-1]
              for c in changed_ids if c in ents}
    shorts = {s for s in shorts if len(s) > 3}          # skip noise names
    name_hit = set()
    test_files = {e.path for e in ents.values() if _is_test_entity(e)}
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
        },
        "note": (f"{len(grounded)} test(s) reached via graph edges; "
                 f"{len(name_hit)} file(s) selected by name only - "
                 f"treat those as guesses"),
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
