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

    def _unreachable_target(self, src_ent, dst_ent) -> str:
        """Why a NAME-ONLY match cannot be the real callee. '' if it can be.

        Bare refs are most of what the parser emits - 6,513 of 6,644 call
        refs on this repo - because a receiver's type is usually unknown.
        resolve() then matches on the last segment alone, and a name with
        exactly ONE definition anywhere wins outright. UNIQUENESS WAS
        BEING READ AS CERTAINTY: the edge was scored 0.95 "exact", above
        the <0.7 line the grounding block warns about, so nothing flagged
        it.

        Measured before this guard: 369 of 1,627 call edges (23%) landed
        on an entity whose short name is a stdlib method the repo never
        defines. Two absorbed 295 of them - `tests.test_console.get`
        collecting every dict.get() in the package, and a one-line stub
        class D inside a test function collecting every Path.read_text().
        That was the hairball in the rendered map, and it inflated fan_in
        and blast radius everywhere those nodes were reached.

        Both rules below are facts about reachability, not name lists. A
        list of stdlib names would be endless, language-specific, and
        wrong the moment a repo legitimately defines `get`.
        """
        # 1. FUNCTION-LOCAL targets. A class or def inside a function body
        #    cannot be named from outside that body - it does not exist
        #    until the function runs, and not by that path afterwards.
        e, hops = dst_ent, 0
        while getattr(e, "parent", None) and hops < 12:
            parent = self.ix.entities.get(e.parent)
            if parent is None:
                break
            if parent.kind in ("function", "method"):
                return "function-local"
            e, hops = parent, hops + 1
        # 2. AN ATTRIBUTE CALL IS NOT A MODULE-LEVEL FUNCTION. `d.get(x)`
        #    cannot be a plain `def get` at module scope, however unique
        #    that name is in the index. The parser knows the call was
        #    written `receiver.name(...)` and used to discard it; RawEdge
        #    carries it now. This is what the first two rules could not
        #    reach: tests calling dict.get() still landed on a test helper
        #    named get, because caller and target were both tests.
        if getattr(self, "_via_attr", False) and dst_ent.kind == "function":
            return "attr-call-to-function"
        # 3. PRODUCTION CODE DOES NOT CALL TEST HELPERS. The same asymmetry
        #    is already relied on by metrics (fan_in excludes test sources)
        #    and by D11b - which only fires on AMBIGUOUS names, and so
        #    could never help here: these names have exactly one definition,
        #    and no competition was read as high confidence when it is the
        #    case that most deserves suspicion.
        from .verify import is_test_entity
        if is_test_entity(dst_ent) and not is_test_entity(src_ent):
            return "test-only"
        return ""

    def build(self):
        self.edges = []
        # counted, not silent: dropping an edge is a claim about the
        # graph and the number belongs where someone can see it.
        self._dropped_unreachable = 0
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
            # A NAME-ONLY match is a guess, and some guesses are provably
            # wrong. Checked here rather than inside resolve() because the
            # rule needs the CALLER, which resolve() has no business knowing.
            self._via_attr = getattr(raw, "via_attr", False)
            if dst_ent is not None and "." not in raw.dst_ref:
                if self._unreachable_target(src_ent, dst_ent):
                    dst_ent = None
                    self._dropped_unreachable += 1
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
                        # guarded like the other short-name lookups: a
                        # declared type is strong evidence, but the CLASS
                        # is still being found by bare name and can land
                        # on a function-local class no annotation could
                        # legally name.
                        cs = [c for c in self._by_short.get(base_short, [])
                              if c.kind == "class"
                              and not self._unreachable_target(src_ent, c)]
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
                elif not cands and self.ix.resolve(
                        raw.dst_ref.rsplit(".", 1)[0]) is not None:
                    # ...and only when the PREFIX names something this repo
                    # actually has. Inheritance dispatch always does - the
                    # ref was built from the calling class - whereas
                    # `subprocess.run` names a module we do not define, and
                    # guessing its last segment against our own entities is
                    # how 77 subprocess.run() call sites became edges into
                    # Harvester.run. Qualifying the receiver in the parser
                    # (schema 6) was only half the fix: this tier stripped
                    # the qualification straight back off.
                    # inheritance dispatch: self.meth resolved to the CALLING
                    # subclass, but meth is defined on an ancestor. Without
                    # base-class info, accept a UNIQUE definition of the bare
                    # method name anywhere in the index - flagged as a guess.
                    # guarded like the other two short-name tiers - this
                    # one was missed on the first pass and kept feeding the
                    # same false hubs, which is the argument for the test
                    # below that enumerates every site rather than trusting
                    # that they were all found.
                    meth = [e for e in self._by_short.get(last, [])
                            if e.kind in ("function", "method")
                            and not self._unreachable_target(src_ent, e)]
                    if len(meth) == 1:
                        dst_ent = meth[0]
                        conf, how = 0.70, "inherited-guess"
            if dst_ent is None and "." not in raw.dst_ref:
                # bare-name fallback: unique short-name match anywhere in
                # the index. Grounded enough to keep WITH a low score -
                # dropping it silently (old behavior) hides real coupling.
                cands = [e for e in self._by_short.get(raw.dst_ref, [])
                         if e.kind in ("function", "method", "class")
                         and not self._unreachable_target(src_ent, e)]
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
    def load(coord_dir: str, write_cache: bool = True) -> list[dict]:
        """write_cache=False makes this a pure read.

        Read-only tools (viz, dig) must not warm .coord/cache/edges.pkl -
        this is the SECOND cache on the load path, and missing it is why
        the viz fence still failed after the coordinates cache was fixed.
        """
        p = Path(coord_dir) / "index" / "edges.json"
        if not p.exists():
            return []
        from .access_cache import load_json_cached
        return load_json_cached(p, Path(coord_dir), write=write_cache)


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
