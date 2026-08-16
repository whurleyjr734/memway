"""
Metrics layer: a numeric shadow of the codebase.

D1: entity extent and complexity come from the PARSER at index time
(Entity.end_lineno / .loc / .complexity). This module never re-derives
spans from source heuristics.

Raw signals only: complexity (radon-validated), churn, fan-in/out.
function+method / attribute), so one outlier cannot compress the scale
and modules do not share a normalizer with methods.

Memoization invariant: every stored value is keyed by (coord_id,
body_hash). Unchanged bodies are never recomputed.
"""

import ast
import json
import re
import textwrap
from pathlib import Path

from .indexer import Indexer

BRANCH_KEYWORDS_JS = re.compile(
    r"\b(if|else if|for|while|case|catch|\?\?|&&|\|\|)\b|\?")


def _py_complexity(body_text: str) -> int:
    try:
        tree = ast.parse(textwrap.dedent(body_text))
    except SyntaxError:
        return 1
    score = 1
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.For, ast.While, ast.AsyncFor,
                             ast.ExceptHandler, ast.With, ast.Assert,
                             ast.comprehension, ast.IfExp)):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
    return score


def complexity_of(body_text: str, path: str) -> int:
    """Called by the indexer at parse time (D1)."""
    if path.endswith(".py"):
        return _py_complexity(body_text)
    return 1 + len(BRANCH_KEYWORDS_JS.findall(body_text))


def _pct_rank(values: dict) -> dict:
    """value dict -> percentile rank dict in [0,1]."""
    if not values:
        return {}
    ordered = sorted(values.values())
    n = len(ordered)
    def rank(v):
        import bisect
        # midpoint of the tie block: robust to heavy ties at the low end
        lo = bisect.bisect_left(ordered, v)
        hi = bisect.bisect_right(ordered, v)
        return ((lo + hi) / 2) / n
    return {k: rank(v) for k, v in values.items()}


class MetricsStore:
    def __init__(self, coord_dir: str):
        self.dir = Path(coord_dir) / "metrics"
        self.file = self.dir / "metrics.json"
        self.data: dict[str, dict] = {}

    def load(self):
        if self.file.exists():
            self.data = json.loads(self.file.read_text())

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        _t = self.file.with_suffix(".tmp")
        _t.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        import os
        os.replace(_t, self.file)

    # ------------------------------------------------------------- compute

    def compute(self, indexer: Indexer, edges: list[dict],
                repo_root: Path) -> dict:
        self.load()
        # D11 refinement: fan-in counts only edges ORIGINATING from
        # production code. Test suites calling a fixture thousands of
        # times says nothing about production attention; test coverage
        # is a separate (positive) signal, not centrality.
        from .verify import is_test_entity
        test_srcs = {cid for cid, ent in indexer.entities.items()
                     if is_test_entity(ent)}
        fan_in, fan_out = {}, {}
        for e in edges:
            if e["kind"] in ("calls", "imports"):
                fan_out[e["src"]] = fan_out.get(e["src"], 0) + 1
                if not str(e["dst"]).startswith("EVT:") \
                        and e["src"] not in test_srcs:
                    fan_in[e["dst"]] = fan_in.get(e["dst"], 0) + 1

        recomputed, memo_hits = 0, 0
        for cid, ent in indexer.entities.items():
            prev = self.data.get(cid)
            lh = getattr(ent, "logic_hash", "")
            if prev and (prev.get("body_hash") == ent.body_hash
                         or (lh and prev.get("logic_hash") == lh)):
                prev["fan_in"] = fan_in.get(cid, 0)
                prev["fan_out"] = fan_out.get(cid, 0)
                prev["line_start"] = ent.lineno
                prev["line_end"] = ent.end_lineno
                memo_hits += 1
                continue
            self.data[cid] = {
                "body_hash": ent.body_hash,
                "logic_hash": getattr(ent, "logic_hash", ""),
                "kind": ent.kind,
                "path": ent.path,
                "line_start": ent.lineno,
                "line_end": ent.end_lineno,
                "loc": ent.loc,
                "complexity": ent.complexity,
                "fan_in": fan_in.get(cid, 0),
                "fan_out": fan_out.get(cid, 0),
                "churn": self.data.get(cid, {}).get("churn", 0),
                "provenance": "computed",
            }
            recomputed += 1

        for cid in list(self.data):
            if cid.startswith("_"):
                continue
            if cid not in indexer.entities:
                del self.data[cid]

        self._rollup(indexer)
        self.save()
        return {"recomputed": recomputed, "memoized": memo_hits}

    def _rollup(self, indexer: Indexer):
        children: dict[str, list[str]] = {}
        for cid, ent in indexer.entities.items():
            if ent.parent:
                children.setdefault(ent.parent, []).append(cid)
        for parent, kids in children.items():
            if parent not in self.data:
                continue
            km = [self.data[k] for k in kids if k in self.data]
            if km:
                self.data[parent]["agg_complexity"] = sum(
                    m["complexity"] for m in km)
                self.data[parent]["n_children"] = len(km)

    # -------------------------------------------------------------- churn

    def apply_churn(self, churn_by_coord: dict[str, int]):
        self.load()
        for cid, n in churn_by_coord.items():
            if cid in self.data:
                self.data[cid]["churn"] = n
        self.save()

    def flag_dirty_tree(self, dirty: bool):
        """D7: metrics describe committed reality; record when the working
        tree differs so consumers know churn/lineage may lag."""
        self.load()
        self.data.setdefault("_meta", {})["dirty_tree"] = dirty
        self.save()

    # ------------------------------------------------------------- lookup

    def triage(self, coord_ids: list[str], top: int = 5) -> list[tuple]:
        self.load()
        ranked = sorted(
            ((cid, self.data.get(cid, {})) for cid in coord_ids),
            key=lambda kv: kv[1].get("complexity", 0), reverse=True)
        return ranked[:top]
