"""
Harvest: mine the knowledge a repo already contains and pre-position it
into the metadata channels. Solves the cold-start problem -- the system
is born knowing everything the codebase already says about itself.

Sources -> channels (all provenance-tagged):
  docstrings            -> docs     (author: mined-docstring)
  git commit history    -> history  (author: mined-git) + churn -> metrics
  git rename detection  -> lineage  (retroactive biography)
  test names            -> notes    (author: mined-test, behavioral contracts)

Provenance ordering (consumers should weight accordingly):
  human-authored > mined-docstring / mined-test > mined-git > computed
"""

import ast
import re
import subprocess
from pathlib import Path

from .indexer import Indexer
from .metadata import MetaStore
from .lineage import VersionStore

TEST_NAME = re.compile(r"^test_?(.+)", re.IGNORECASE)


def _git(repo: Path, *args) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


class Harvester:
    def __init__(self, indexer: Indexer, meta: MetaStore,
                 versions: VersionStore, repo_root: Path):
        self.ix = indexer
        self.meta = meta
        self.versions = versions
        self.repo = Path(repo_root)

    def run(self) -> dict:
        stats = {"docstrings": 0, "git_history": 0,
                 "test_contracts": 0, "retro_lineage": 0}
        stats["docstrings"] = self.harvest_docstrings()
        g, lin = self.harvest_git()
        stats["git_history"] = g
        stats["retro_lineage"] = lin
        stats["test_contracts"] = self.harvest_tests()
        return stats

    # --------------------------------------------------------- docstrings

    def harvest_docstrings(self) -> int:
        count = 0
        by_file: dict[str, list] = {}
        for cid, ent in self.ix.entities.items():
            by_file.setdefault(ent.path, []).append((cid, ent))

        for path, ents in by_file.items():
            if not path.endswith(".py"):
                continue
            try:
                tree = ast.parse((self.repo / path).read_text())
            except (OSError, SyntaxError):
                continue
            qual_by_line = {e.lineno: (cid, e) for cid, e in ents}

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef, ast.Module)):
                    doc = ast.get_docstring(node)
                    if not doc:
                        continue
                    lineno = getattr(node, "lineno", 1)
                    hit = qual_by_line.get(lineno)
                    if hit and not self._already(hit[0], "docs",
                                                 "mined-docstring"):
                        self.meta.add(hit[0], "docs", doc.strip(),
                                      author="mined-docstring")
                        count += 1
        return count

    # ---------------------------------------------------------------- git

    def harvest_git(self) -> tuple[int, int]:
        if not (self.repo / ".git").exists():
            return 0, 0
        hist_count, lineage_count = 0, 0
        files = {e.path for e in self.ix.entities.values()}

        for path in sorted(files):
            log = _git(self.repo, "log", "--follow",
                       "--format=%h|%ad|%s", "--date=short", "--", path)
            if not log.strip():
                continue
            commits = [l.split("|", 2) for l in log.strip().splitlines()]

            # attach condensed history to the module coordinate
            mod_ent = next((cid for cid, e in self.ix.entities.items()
                            if e.path == path and e.kind == "module"), None)
            if mod_ent and not self._already(mod_ent, "history", "mined-git"):
                summary = "; ".join(
                    f"{c[1]} {c[2]}" for c in commits[:8])
                if len(commits) > 8:
                    summary += f" (+{len(commits) - 8} earlier)"
                self.meta.add(mod_ent, "history",
                              f"git: {len(commits)} commits. {summary}",
                              author="mined-git")
                hist_count += 1

            # retroactive lineage: renames git already knows about
            renames = _git(self.repo, "log", "--follow", "--name-status",
                           "--format=", "--", path)
            existing = {(tuple(r["old"]), tuple(r["new"]))
                        for r in self.versions.read()
                        if r.get("author") == "mined-git"}
            for line in renames.splitlines():
                if line.startswith("R"):
                    parts = line.split("\t")
                    if len(parts) == 3:
                        key = ((f"git:{parts[1]}",), (f"git:{parts[2]}",))
                        if key in existing:
                            lineage_count -= 0
                            continue
                        existing.add(key)
                        self.versions.record(
                            "renamed", [f"git:{parts[1]}"],
                            [f"git:{parts[2]}"],
                            note="retroactive, from git --follow",
                            author="mined-git")
                        lineage_count += 1

        # D2: churn via ONE `git blame --line-porcelain` pass per file.
        # Churn(entity) = distinct commits that authored the entity's
        # current lines (exact spans from D1). One subprocess per file
        # instead of one `git log -L` per entity. Note the semantic
        # shift vs -L: blame measures authorship of surviving lines,
        # not every historical touch of the span; rankings correlate,
        # absolute values differ (documented in the repair guide).
        by_file: dict[str, list] = {}
        for cid, e in self.ix.entities.items():
            by_file.setdefault(e.path, []).append((cid, e))

        churn = {}
        for path, ents in by_file.items():
            blame = _git(self.repo, "blame", "--line-porcelain", path)
            if not blame:
                continue
            line_commit = []
            for bl in blame.splitlines():
                parts = bl.split()
                if len(parts) >= 3 and len(parts[0]) == 40 \
                        and all(c in "0123456789abcdef" for c in parts[0]):
                    line_commit.append(parts[0])
            for cid, e in ents:
                if e.kind == "module":
                    churn[cid] = len(set(line_commit))
                else:
                    span = line_commit[e.lineno - 1:
                                       e.end_lineno or e.lineno]
                    churn[cid] = len(set(span))
        if churn:
            from .metrics import MetricsStore
            MetricsStore(self.versions.coord_dir).apply_churn(churn)
        return hist_count, lineage_count

    # -------------------------------------------------------------- tests

    def harvest_tests(self) -> int:
        """D5: ordered mining strategies, distinct provenance per strategy.
        (1) name-match: highest confidence, contract must name its target.
        (2) assertion-target: the entity whose call result a test asserts
            on is what the test is ABOUT, even when names don't align."""
        count = self._mine_by_name() + self._mine_by_assertion()
        return count

    def _mine_by_name(self) -> int:
        count = 0
        # map: called entity -> test entities that call it
        for cid, ent in self.ix.entities.items():
            m = TEST_NAME.match(ent.qualname.rsplit(".", 1)[-1])
            if not m or ent.kind not in ("function", "method"):
                continue
            contract = m.group(1).replace("_", " ")
            contract_words = set(contract.split())
            for edge in getattr(self.ix, "_raw_edges", []):
                if edge.src_qualname == ent.qualname \
                        and edge.kind == "calls":
                    target = self.ix.resolve(edge.dst_ref)
                    # attach only where the contract is actually ABOUT the
                    # target: its short name must appear in the contract.
                    # Prevents constructor calls dragging class coords in.
                    if not target:
                        continue
                    short = target.qualname.rsplit(".", 1)[-1].lower()
                    if short not in contract_words:
                        continue
                    if self._already(target.coord_id, "notes", "mined-test"):
                        continue
                    self.meta.add(
                        target.coord_id, "notes",
                        f"behavioral contract (from {ent.qualname}): "
                        f"{contract}",
                        author="mined-test")
                    count += 1
        return count

    def _mine_by_assertion(self) -> int:
        count = 0
        test_files = {e.path for e in self.ix.entities.values()
                      if "test" in e.path.lower()
                      and e.path.endswith(".py")}
        for path in sorted(test_files):
            try:
                tree = ast.parse((self.repo / path).read_text())
            except (OSError, SyntaxError):
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef,
                                       ast.AsyncFunctionDef)):
                    continue
                m = TEST_NAME.match(fn.name)
                if not m:
                    continue
                contract = m.group(1).replace("_", " ")
                # calls whose results feed assert statements
                targets = set()
                for node in ast.walk(fn):
                    if isinstance(node, ast.Assert):
                        for call in ast.walk(node):
                            if isinstance(call, ast.Call):
                                fnode = call.func
                                nm = (fnode.id if isinstance(fnode, ast.Name)
                                      else fnode.attr
                                      if isinstance(fnode, ast.Attribute)
                                      else None)
                                if nm:
                                    targets.add(nm)
                for nm in targets:
                    tgt = self.ix.resolve(nm)
                    if not tgt or tgt.kind not in ("function", "method"):
                        continue
                    if "test" in tgt.path.lower():
                        continue      # never attach contracts to tests
                    if self._already(tgt.coord_id, "notes",
                                     "mined-test-assert"):
                        continue
                    self.meta.add(
                        tgt.coord_id, "notes",
                        f"asserted behavior (from {fn.name}): {contract}",
                        author="mined-test-assert")
                    count += 1
        return count

    # ------------------------------------------------------------- helper

    def _already(self, cid: str, channel: str, author: str) -> bool:
        return any(e.get("author") == author
                   for e in self.meta.read(cid, channel))


_DOC_BINDINGS = "docbindings.json"


def harvest_docs(repo_root, ix, coord_dir, write: bool = True):
    """Bind design documents to the coordinates they govern.

    Scans docs/**/*.md for backticked entity references (explicit refs
    only - fuzzy prose matching stays out, per the manufactured-edge
    post-mortem in parsers.py). Each binding snapshots the entity's
    logic hash at the doc's last touch: when governed logic drifts past
    that snapshot, the DOC is flagged - architecture-decision rot
    detection. Docs re-scan only when their file hash changes.
    """
    import hashlib
    import json
    import re
    from pathlib import Path
    repo_root, coord_dir = Path(repo_root), Path(coord_dir)
    path = coord_dir / _DOC_BINDINGS
    old = {}
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            old = {}
    out = {}
    docs_dir = repo_root / "docs"
    if docs_dir.is_dir():
        for md in sorted(docs_dir.rglob("*.md")):
            # Skip docs/**/examples/** - documentation OF the tool, not
            # design docs GOVERNING entities. A repo publishing a site
            # from /docs accumulates plenty of the former, and binding it
            # dirties docbindings.json on every reindex, which teaches
            # people to ignore a dirty map.
            if "examples" in md.relative_to(docs_dir).parts[:-1]:
                continue
            rel = str(md.relative_to(repo_root))
            try:
                text = md.read_text()
            except OSError:
                continue
            fh = hashlib.sha256(text.encode()).hexdigest()[:16]
            prev = old.get(rel)
            if prev and prev.get("file_hash") == fh:
                out[rel] = prev
                continue
            refs = {}
            for tok in sorted(set(re.findall(r"`([A-Za-z_][\w.]*)`", text))):
                e = ix.resolve(tok)
                if e is not None:
                    refs[e.coord_id] = {
                        "qualname": e.qualname,
                        "logic_hash": getattr(e, "logic_hash", "")
                                      or e.body_hash}
            if refs:
                out[rel] = {"file_hash": fh, "refs": refs}
    # write=False exists for read-only surfaces. This rewrote
    # docbindings.json on EVERY before_edit - from the CLI and MCP too -
    # so a pure query dirtied the map; nobody noticed until the console
    # fingerprinted .coord around a GET.
    #
    # But note what this file IS: not a cache. It snapshots the entity
    # hash a doc was written against, and drift
    # ("entity-changed-since-doc") is measured from that snapshot.
    # Suppressing the write UNCONDITIONALLY makes every binding look
    # permanently fresh - two tests caught exactly that. "Derived" is
    # two categories: regenerable-from-source (cache, evidence) and
    # snapshot-baseline (this, versions/). Only the first is safe to
    # skip on a read.
    if write:
        try:
            path.write_text(json.dumps(out, indent=1))
        except OSError:
            pass
    return out
