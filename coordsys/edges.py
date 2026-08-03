"""
Edge extraction: turns the coordinate index into a graph.

Edge kinds:
  contains   - structural parent/child (module contains class, etc.)
  imports    - module-level imports
  calls      - function/method A calls B (best-effort static resolution)
  emits      - entity publishes an event   (via emit()/publish() calls)
  consumes   - entity subscribes to event  (via subscribe()/on() calls)

Event edges make the system aware of event-driven architecture: emitters
and consumers of the same event name get linked through an event node.
"""

import json
from pathlib import Path
from .indexer import Indexer

EVENT_EMIT_FUNCS = {"emit", "publish", "dispatch"}
EVENT_SUB_FUNCS = {"subscribe", "on", "consume", "register_handler"}


class EdgeBuilder:
    """Resolves the raw edges collected during indexing against the
    coordinate index. Language plugins emit RawEdge refs (qualnames,
    bare names, EVT:<name>); this class turns them into coordinate IDs."""

    def __init__(self, indexer: Indexer):
        self.ix = indexer
        self.edges: list[dict] = []
        self._seen: set = set()

    def _hierarchy(self):
        """class qualname -> resolved base-class qualnames (one AST pass).
        Lets resolution walk real MROs instead of guessing on method names."""
        import ast as _ast
        from pathlib import Path
        bases, class_short = {}, {}
        root = Path(self.ix.repo_root)
        for p in root.rglob("*.py"):
            # skip virtualenvs, hidden dirs, and vendored trees - a repo
            # root carrying an embedded .venv would otherwise AST-parse
            # thousands of site-packages files (found live on a user
            # machine; container test repos never carried a venv)
            rel_parts = p.relative_to(root).parts
            if any(part.startswith(".") or part in
                   ("site-packages", "node_modules", "venv", "__pycache__")
                   for part in rel_parts[:-1]):
                continue
            mod = str(p.relative_to(root))[:-3].replace("/", ".").replace("\\", ".")
            try:
                tree = _ast.parse(p.read_text())
            except Exception:
                continue
            def walk(node, prefix):
                for ch in _ast.iter_child_nodes(node):
                    if isinstance(ch, _ast.ClassDef):
                        q = f"{prefix}{ch.name}"
                        bn = []
                        for b in ch.bases:
                            if isinstance(b, _ast.Name):
                                bn.append(b.id)
                            elif isinstance(b, _ast.Attribute):
                                bn.append(b.attr)
                        bases[q] = bn
                        class_short.setdefault(ch.name, []).append(q)
                        walk(ch, q + ".")
                    elif isinstance(ch, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                        walk(ch, f"{prefix}{ch.name}.")
            walk(tree, mod + ".")
        resolved = {}
        for q, bn in bases.items():
            pkg = q.rsplit(".", 2)[0]
            rs = []
            for b in bn:
                cands = class_short.get(b, [])
                if len(cands) == 1:
                    rs.append(cands[0])
                elif cands:
                    same = [c for c in cands if c.startswith(pkg)]
                    if len(same) == 1:
                        rs.append(same[0])
            resolved[q] = rs
        return resolved

    def _mro_resolve(self, cls_q, meth):
        """First ancestor of cls_q (BFS) defining meth, or None."""
        seen, q = {cls_q}, list(self._bases.get(cls_q, []))
        while q:
            anc = q.pop(0)
            if anc in seen:
                continue
            seen.add(anc)
            ent = self.ix.resolve(f"{anc}.{meth}")
            if ent:
                return ent
            q.extend(self._bases.get(anc, []))
        return None

    def build(self):
        self.edges = []
        self._bases = self._hierarchy()
        # materialize class hierarchy as structural edges: until now the
        # MRO data was consumed during resolution and thrown away - as
        # edges, agents can ASK about inheritance (bases, subclasses,
        # overrides) and knowledge can flow down the hierarchy.
        for cls_q, base_qs in self._bases.items():
            ce = self.ix.resolve(cls_q)
            if not ce:
                continue
            for bq in base_qs:
                be = self.ix.resolve(bq)
                if be and be.coord_id != ce.coord_id:
                    self._add(ce.coord_id, be.coord_id, "inherits",
                              confidence=1.0, resolution="structural")
        # O(1) lookup indexes (bare-name scan was O(raw_edges x entities))
        self._by_short = {}
        self._by_last = {}
        for e in self.ix.entities.values():
            short = e.qualname.rsplit(".", 1)[-1]
            self._by_short.setdefault(short, []).append(e)
            self._by_last.setdefault(short, []).append(e)

        # containment from the index itself (structural: certain)
        for cid, e in self.ix.entities.items():
            if e.parent:
                self._add(e.parent, cid, "contains",
                          confidence=1.0, resolution="structural")

        for raw in getattr(self.ix, "_raw_edges", []):
            src_ent = self.ix.resolve(raw.src_qualname)
            if not src_ent:
                continue
            if raw.dst_ref.startswith("EVT:"):
                # dynamic dispatch: string-named events are the least
                # grounded edge kind we emit
                self._add(src_ent.coord_id, raw.dst_ref, raw.kind,
                          confidence=0.5, resolution="event")
                continue
            dst_ent = self.ix.resolve(raw.dst_ref)
            conf, how = 0.95, "exact"
            if dst_ent is None and "." in raw.dst_ref:
                # inheritance dispatch, resolved via REAL class hierarchy:
                # self.meth was attributed to the calling subclass, but meth
                # is defined on an ancestor - walk the MRO to find it.
                cls_q, meth = raw.dst_ref.rsplit(".", 1)
                m = self._mro_resolve(cls_q, meth)
                if m:
                    dst_ent, conf, how = m, 0.90, "mro"
            if dst_ent is None and "." in raw.dst_ref:
                # ANNOTATED receiver: the author declared the type. If the
                # ref's first segment is a parameter of the calling entity
                # with an annotation, resolve the method against that class
                # (directly or up its MRO). Ground truth beats guessing:
                # every edge this wins was previously a guess or a drop.
                recv, rest = raw.dst_ref.split(".", 1)
                ann = (getattr(src_ent, "param_types", None) or {}).get(recv)
                if ann:
                    base = ann.split("[")[0].strip().strip("'\"")
                    if base == "Optional" and "[" in ann:
                        base = ann.split("[", 1)[1].rstrip("]").split(",")[0].strip()
                    base_short = base.rsplit(".", 1)[-1]
                    cls_e = self.ix.resolve(base)
                    if cls_e is None or cls_e.kind != "class":
                        cs = [c for c in self._by_short.get(base_short, [])
                              if c.kind == "class"]
                        cls_e = cs[0] if len(cs) == 1 else None
                    if cls_e is not None and cls_e.kind == "class":
                        meth = rest.split(".", 1)[0]
                        target = self.ix.resolve(f"{cls_e.qualname}.{meth}") \
                            or self._mro_resolve(cls_e.qualname, meth)
                        if target:
                            dst_ent, conf, how = target, 0.90, "annotated"
            if dst_ent is None and "." in raw.dst_ref:
                # dotted ref from scope-aware parsing ("Session.request"):
                # accept a UNIQUE qualname-suffix match; ambiguity still
                # drops the edge (conservatism preserved).
                suffix = "." + raw.dst_ref
                last = raw.dst_ref.rsplit(".", 1)[-1]
                cands = [e for e in self._by_last.get(last, [])
                         if e.qualname.endswith(suffix)
                         or e.qualname == raw.dst_ref]
                if len(cands) == 1:
                    dst_ent = cands[0]
                    conf, how = 0.75, "suffix"
                elif not cands:
                    # inheritance dispatch: self.meth resolved to the CALLING
                    # subclass, but meth is defined on an ancestor. Without
                    # base-class info, accept a UNIQUE definition of the bare
                    # method name anywhere in the index - flagged as a guess.
                    meth = [e for e in self._by_short.get(last, [])
                            if e.kind in ("function", "method")]
                    if len(meth) == 1:
                        dst_ent = meth[0]
                        conf, how = 0.70, "inherited-guess"
            if dst_ent is None and "." not in raw.dst_ref:
                # bare-name fallback: unique short-name match anywhere in
                # the index. Grounded enough to keep WITH a low score -
                # dropping it silently (old behavior) hides real coupling.
                cands = [e for e in self._by_short.get(raw.dst_ref, [])
                         if e.kind in ("function", "method", "class")]
                if len(cands) == 1:
                    dst_ent = cands[0]
                    conf, how = 0.60, "bare-name"
            if dst_ent and dst_ent.coord_id != src_ent.coord_id:
                self._add(src_ent.coord_id, dst_ent.coord_id, raw.kind,
                          confidence=conf, resolution=how)

        return self.edges

    def _add(self, src, dst, kind, **attrs):
        key = (src, dst, kind, tuple(sorted(attrs.items())))
        if key in self._seen:          # Phase A: set dedup, was O(edges^2)
            return
        self._seen.add(key)
        edge = {"src": src, "dst": dst, "kind": kind}
        edge.update(attrs)
        self.edges.append(edge)

    def save(self, coord_dir: str):
        idx_dir = Path(coord_dir) / "index"
        idx_dir.mkdir(parents=True, exist_ok=True)
        _t = idx_dir / "edges.tmp"
        _t.write_text(json.dumps(self.edges, indent=2))
        import os
        os.replace(_t, idx_dir / "edges.json")

    @staticmethod
    def load(coord_dir: str) -> list[dict]:
        p = Path(coord_dir) / "index" / "edges.json"
        if not p.exists():
            return []
        from .access_cache import load_json_cached
        return load_json_cached(p, Path(coord_dir))


def neighbors(edges: list[dict], coord_id: str) -> list[dict]:
    """All edges touching a coordinate."""
    return [e for e in edges if e["src"] == coord_id or e["dst"] == coord_id]


def event_pairs(edges: list[dict]) -> dict:
    """Map event name -> {'emitters': [...], 'consumers': [...]}."""
    events: dict[str, dict] = {}
    for e in edges:
        if isinstance(e["dst"], str) and e["dst"].startswith("EVT:"):
            name = e["dst"][4:]
            ev = events.setdefault(name, {"emitters": [], "consumers": []})
            if e["kind"] == "emits":
                ev["emitters"].append(e["src"])
            elif e["kind"] == "consumes":
                ev["consumers"].append(e["src"])
    return events
