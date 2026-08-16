"""Structured query API: the map's facts as data, not formatted text.

Both `memway <cmd> --json` and the MCP server call these functions.
The CLI's human-readable printers are untouched; this module is the
one structured surface, so agents and humans never drift apart.

Every function takes a loaded context and returns JSON-serializable
dicts/lists. Errors are returned as {"error": "..."} rather than
raised, so an agent gets a usable message instead of a stack trace.

Context loading is shared via _ctx(); edge grounding (resolution provenance
and confidence) is surfaced in before_edit to calibrate trust in static analysis.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path

from .indexer import Indexer
from .edges import EdgeBuilder, neighbors
from .metadata import MetaStore, stamp_for, accepted_for
from .metrics import MetricsStore
from .lineage import VersionStore


# _ctx NEVER warms a pickle cache - `memway index` writes them, reads
# consume them. read_only() remains for the SNAPSHOT baselines
# (docbindings), which are a different category: skipping those on a
# read is only safe when the caller is explicitly read-only.
# When true, _ctx loads without warming any pickle cache. The console
# serves these same query functions over HTTP and promises every GET
# leaves .coord byte-identical; there are exactly TWO cache-warming
# loaders on this path (coordinates.pkl and edges.pkl) and missing either
# breaks the fence. Toggled by `read_only()`, never left on globally -
# the CLI and MCP still want a warm cache.
_READ_ONLY = False


@contextmanager
def read_only():
    """Serve queries without writing anything under .coord."""
    global _READ_ONLY
    prev = _READ_ONLY
    _READ_ONLY = True
    try:
        yield
    finally:
        _READ_ONLY = prev


def _ctx(repo: str):
    """Load the map once; shared by every query."""
    repo_p = Path(repo).resolve()
    coord = repo_p / ".coord"
    if not (coord / "index" / "coordinates.json").exists():
        return None
    ix = Indexer(repo_p, coord)
    ix.load_existing(write_cache=False)
    ix.load_raw_edges()
    edges = EdgeBuilder.load(coord, write_cache=False)
    meta = MetaStore(coord)
    return repo_p, coord, ix, edges, meta


def _entity_dict(e, meta=None) -> dict:
    d = {
        "coord_id": e.coord_id,
        "qualname": e.qualname,
        "kind": e.kind,
        "path": e.path,
        "line": e.lineno,
        "line_end": getattr(e, "end_lineno", 0),
        "signature": e.signature,
    }
    if meta is not None:
        md = meta.read_all(e.coord_id,
                       current_hash=accepted_for(e))
        knowledge = []
        for channel, entries in md.items():
            for en in entries:
                knowledge.append({
                    "channel": channel,
                    "text": en["text"],
                    "stale": bool(en.get("stale")),
                    "author": en.get("author", ""),
                })
        d["knowledge"] = knowledge
        _attach_evidence(d, e, meta)
    return d


def _attach_evidence(d: dict, e, meta) -> None:
    """Join verdicts to their evidence and summarise what is cached.

    READ ONLY. Evidence is written by `dig --cache` and by nothing else -
    a briefing that silently populated a cache would make every read a
    write, which is exactly the fence this project keeps rediscovering.
    """
    from . import evidence as ev
    coord = Path(meta.root).parent if hasattr(meta, "root") else None
    if coord is None:
        return
    records = ev.read(coord, e.coord_id)
    ev.decorate_knowledge(d.get("knowledge", []), records)
    if records:
        d["evidence"] = ev.summarise(records)


_COORD_REF = re.compile(r"^C-[0-9a-fA-F]{4,}$")


def _supersession(ref: str, coord) -> list:
    """Follow a retired coordinate id forward through the lineage store.

    A rename mints a NEW coordinate id and migrates metadata to it; the
    old id simply stops resolving. So any id written down OUTSIDE the
    index - a PR comment, a design doc, an agent's memory - dies at the
    next refactor, which is precisely when the map is most wanted.

    lineage.jsonl records old -> new explicitly, so this is an exact
    forward pointer rather than a guess. Returns the chain of hops
    [{"from","to","note"}], oldest first; empty if ref was never retired.
    """
    path = Path(coord) / "lineage" / "lineage.jsonl"
    if not path.exists():
        return []
    fwd = {}
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            olds, news = r.get("old") or [], r.get("new") or []
            if len(olds) == 1 and len(news) == 1:
                fwd[olds[0]] = (news[0], r.get("note", ""))
    except (OSError, ValueError):
        return []                       # a damaged log is not an error here
    chain, seen, cur = [], {ref}, ref
    while cur in fwd:
        nxt, note = fwd[cur]
        if nxt in seen:
            break                       # defensive: cycle in the log
        chain.append({"from": cur, "to": nxt, "note": note})
        seen.add(nxt)
        cur = nxt
    return chain


def _resolve_with_lineage(ref: str, ix, coord):
    """ix.resolve(), then follow supersession for retired coordinate ids.

    Returns (entity_or_None, hops). Only coordinate-shaped refs are
    chased: a qualname that no longer exists is already served well by
    the fuzzy `closest` list, but a hex id scored by string similarity
    produces pure noise.
    """
    e = ix.resolve(ref)
    if e is not None or coord is None or not _COORD_REF.match(str(ref)):
        return e, []
    hops = _supersession(str(ref), coord)
    if not hops:
        return None, []
    return ix.resolve(hops[-1]["to"]), hops


def _mark_superseded(out: dict, ref: str, e, hops: list) -> dict:
    """Annotate a payload served for a retired id, never silently."""
    if hops:
        out["superseded_from"] = ref
        out["supersession"] = hops
        out["note"] = (f"{ref} is retired; superseded by {e.coord_id} "
                       f"({e.qualname}) - payload below is the successor's")
    return out


def _resolve_error(ref: str, ix, coord=None) -> dict:
    """Generate an actionable error when ref doesn't resolve.
    Returns top-3 fuzzy qualname matches and a hint on how to resolve."""
    from difflib import SequenceMatcher

    if coord is not None and _COORD_REF.match(str(ref)):
        hops = _supersession(str(ref), coord)
        if hops:
            last = hops[-1]["to"]
            return {
                "error": f"coordinate {ref!r} is retired, not unknown",
                "superseded_by": last,
                "supersession": hops,
                "hint": f"re-query with {last}",
            }

    # Find fuzzy matches
    all_qualnames = list(ix.by_qualname.keys())
    scored = []
    for qn in all_qualnames:
        ratio = SequenceMatcher(None, ref, qn).ratio()
        scored.append((ratio, qn))
    scored.sort(reverse=True)
    closest = [qn for _, qn in scored[:3]]

    return {
        "error": f"no entity matches {ref!r}",
        "closest": closest,
        "hint": "try memway_at <file:line> or a bare function name"
    }


# --------------------------------------------------------------- queries

def show(repo: str, ref: str) -> dict:
    ctx = _ctx(repo)
    if not ctx:
        return {"error": f"no index at {repo}; run memway init first"}
    _, coord, ix, edges, meta = ctx
    e, _hops = _resolve_with_lineage(ref, ix, coord)
    if not e:
        return _resolve_error(ref, ix, coord)
    out = _mark_superseded(_entity_dict(e, meta), ref, e, _hops)
    rel = []
    for edge in neighbors(edges, e.coord_id):
        other = edge["dst"] if edge["src"] == e.coord_id else edge["src"]
        direction = "out" if edge["src"] == e.coord_id else "in"
        oe = ix.entities.get(other)
        rel.append({
            "direction": direction,
            "kind": edge["kind"],
            "coord_id": other if not str(other).startswith("EVT:") else None,
            "target": ix.entities[other].qualname if oe
                      else str(other),
            # how this edge was established. Runtime-recorded edges (from
            # probe) are indistinguishable from statically-derived ones
            # without this, and a reader that cannot tell will assume the
            # source says what the graph says - which for dynamic dispatch
            # is exactly backwards.
            "resolution": edge.get("resolution", "unknown"),
            "confidence": edge.get("confidence", 1.0),
        })
    out["edges"] = rel
    return out


def lineage(repo: str, ref: str) -> dict:
    ctx = _ctx(repo)
    if not ctx:
        return {"error": f"no index at {repo}; run memway init first"}
    _, coord, ix, _, _ = ctx
    e = ix.resolve(ref)
    cid = e.coord_id if e else ref
    chain = VersionStore(coord).ancestry(cid)
    return {"coord_id": cid,
            "history": [{"version": r["version"], "kind": r["kind"],
                         "note": r.get("note", "")} for r in chain]}


def summary(repo: str) -> dict:
    """The orchestrator's briefing: repo shape at a glance - what an
    agent should read FIRST instead of grepping."""
    ctx = _ctx(repo)
    if not ctx:
        return {"error": f"no index at {repo}; run memway init first"}
    _, coord, ix, edges, meta = ctx
    from collections import Counter
    langs = Counter()
    for e in ix.entities.values():
        if e.kind == "module":
            ext = Path(e.path).suffix
            langs[ext] += 1
    kinds = Counter(e.kind for e in ix.entities.values())
    ms = MetricsStore(coord)
    ms.load()
    # THE test/source split is verify.is_test_entity - path and filename,
    # never the qualname. This was `"test" not in e.path.lower()`, a
    # substring match that would have dropped any source file whose path
    # merely contains the letters (memway/latest.py, contest/, protest/)
    # from the hardest list, silently and forever.
    #
    # PRESENTATION ONLY. Nothing here reads or writes a metric; the same
    # numbers are partitioned two ways, and a test asserts the metrics
    # store is byte-identical across a summary call.
    from .verify import is_test_entity
    ranked = [(ms.data.get(cid, {}).get("complexity", 0), e.qualname,
               is_test_entity(e))
              for cid, e in ix.entities.items()
              if e.kind in ("function", "method")]
    ranked.sort(key=lambda r: (-r[0], r[1]))
    # the flag is CARRIED, never hardcoded: a literal False here made
    # `hardest` unable to report a test even if the filter let one in,
    # which made the test asserting it vacuous. Falsification caught it.
    prod = [(c, q, t) for c, q, t in ranked if not t]
    n_src = sum(1 for e in ix.entities.values() if not is_test_entity(e))

    # ---- knowledge census: what does the map remember? ----
    # Repo-wide answer to "what has been learned here", so orientation
    # is one summary call, not a hand-walk through .coord/meta/.
    # Walks the coordinates that actually have metadata on disk and
    # reads each channel THROUGH MetaStore.read, judging freshness the
    # same way before_edit does: an entry is fresh if its stamp matches
    # the entity's current logic_hash or body_hash. Channel discovery is
    # per-directory (f.stem), not the CHANNELS tuple, so channels added
    # later (e.g. confirm) are censused without touching this code.
    #
    # Distinction: coordinates that don't resolve to live entities fall
    # into two classes - superseded (their knowledge migrated to a
    # successor in the lineage chain) vs orphaned (genuinely lost, no
    # successor). Superseded coordinates are excluded from totals to
    # prevent double-counting migration receipts.
    vs = VersionStore(coord)
    chan_counts = Counter()
    know = []
    total_entries = 0
    superseded_count = 0
    if meta.root.exists():
        for cdir in sorted(meta.root.iterdir()):
            if not cdir.is_dir():
                continue
            cid = cdir.name
            e = ix.entities.get(cid)
            accepted = ({getattr(e, "logic_hash", ""), e.body_hash}
                        if e else "")
            channels, any_stale, n_here = [], False, 0
            for f in sorted(cdir.glob("*.jsonl")):
                entries = meta.read(cid, f.stem, accepted)
                if not entries:
                    continue
                channels.append(f.stem)
                n_here += len(entries)
                if any(en.get("stale") for en in entries):
                    any_stale = True
            if not n_here:
                continue
            # If coordinate doesn't resolve, check if it's superseded
            if not e:
                # Check lineage for a successor: cid in any "old" list
                has_successor = any(cid in entry.get("old", [])
                                   for entry in vs.read())
                if has_successor:
                    # Superseded: exclude from totals, don't add to know list
                    superseded_count += 1
                    continue
            # Live entity or orphaned: count it
            for f in sorted(cdir.glob("*.jsonl")):
                entries = meta.read(cid, f.stem, accepted)
                if entries:
                    chan_counts[f.stem] += len(entries)
            total_entries += n_here
            know.append({
                "coordinate": cid,
                # an unresolvable coordinate means orphaned knowledge
                # (entity gone without lineage migration): surface it,
                # and flag it - freshness cannot be verified.
                "qualname": e.qualname if e else None,
                "channels": channels,
                "entries": n_here,
                "any_stale": any_stale if e else True,
            })
    know.sort(key=lambda k: (not k["any_stale"], k["qualname"] or ""))

    return {
        "entities": len(ix.entities),
        "edges": len(edges),
        "languages": dict(langs),
        "kinds": dict(kinds),
        # `hardest` keeps its meaning exactly - source only - because
        # consumers already depend on it. is_test rides along so every
        # entry in both lists has the same shape. `hardest_overall` is
        # the new, additive view: the same numbers, nothing excluded.
        "hardest": [{"qualname": q, "complexity": c, "is_test": t}
                    for c, q, t in prod[:5]],
        "hardest_overall": [{"qualname": q, "complexity": c, "is_test": t}
                            for c, q, t in ranked[:5]],
        "entities_by_origin": {"source": n_src,
                               "tests": len(ix.entities) - n_src},
        "knowledge": {
            "total_entries": total_entries,
            "coordinates_with_knowledge": len(know),
            "by_channel": dict(chan_counts),
            "superseded": superseded_count,
            "entries": know[:20],
        },
    }


def before_edit(repo: str, ref: str) -> dict:
    """The pre-change safety briefing: everything a developer should
    see BEFORE modifying an entity, in one call - what it is, who
    depends on it, what could break, and what the team wrote about it.
    Composed entirely from existing map facts; adds judgment as
    explicit WARNINGS, never hides the data behind them."""
    ctx = _ctx(repo)
    if not ctx:
        return {"error": f"no index at {repo}; run memway init first"}
    repo_p, coord, ix, edges, meta = ctx
    e, _hops = _resolve_with_lineage(ref, ix, coord)
    if not e:
        return _resolve_error(ref, ix, coord)

    ms = MetricsStore(coord)
    ms.load()
    m = ms.data.get(e.coord_id, {})

    callers = []
    for edge in edges:
        if edge["kind"] == "calls" and edge["dst"] == e.coord_id:
            src = ix.entities.get(edge["src"])
            if src:
                callers.append({"qualname": src.qualname,
                                "path": src.path, "line": src.lineno})

    from .blast import blast_radius
    b = blast_radius([e.coord_id], edges)
    affected = []
    for cid in b.get("affected", {}):
        ae = ix.entities.get(cid)
        if ae:
            affected.append({"qualname": ae.qualname,
                             "depth": b["depths"][cid],
                             "via_event": cid in b["via_event"]})
    affected.sort(key=lambda a: (a["depth"], a["qualname"]))
    radius = {
        "downstream_count": len(affected),
        "direct": [a["qualname"] for a in affected if a["depth"] == 1],
        "transitive_count": sum(1 for a in affected if a["depth"] > 1),
        "via_event": [a["qualname"] for a in affected if a["via_event"]],
        "is_lower_bound": b.get("radius_is_lower_bound", False),
    }

    # Grounding: how trustworthy is this picture? Every edge carries the
    # provenance of how it was resolved (exact / mro / suffix /
    # inherited-guess / bare-name / event / runtime) AND a confidence score.
    # We tally resolution methods and count low-confidence edges (<0.7),
    # surfacing both so the caller can calibrate trust instead of treating
    # the radius as gospel.
    aff_ids = set(b.get("affected", {})) | {e.coord_id}
    res_counts: dict = {}
    low_conf = 0
    for edge in edges:
        if edge["kind"] in ("calls", "imports") and (
                edge["dst"] in aff_ids or edge["src"] in aff_ids):
            how = edge.get("resolution", "exact")
            res_counts[how] = res_counts.get(how, 0) + 1
            if edge.get("confidence", 1.0) < 0.7:
                low_conf += 1
    grounding = {
        "edge_resolution": res_counts,
        "low_confidence_edges": low_conf,
        "note": ("all edges in this radius are static-resolved"
                 if low_conf == 0 else
                 f"{low_conf} edge(s) in this radius are guesses "
                 "(bare-name/inherited/event resolution) - verify "
                 "before trusting the counts"),
    }

    # ---- inheritance: bases, subclasses, overrides, dispatch shape ----
    inh_up = {}      # class cid -> [base cids]
    inh_down = {}    # class cid -> [subclass cids]
    for edge in edges:
        if edge["kind"] == "inherits":
            inh_up.setdefault(edge["src"], []).append(edge["dst"])
            inh_down.setdefault(edge["dst"], []).append(edge["src"])

    def _mro(cls_cid):
        """Ancestor class cids, BFS order (approximate MRO)."""
        out, q_ = [], list(inh_up.get(cls_cid, []))
        seen = {cls_cid}
        while q_:
            c = q_.pop(0)
            if c in seen:
                continue
            seen.add(c)
            out.append(c)
            q_.extend(inh_up.get(c, []))
        return out

    def _descendants(cls_cid):
        out, q_ = [], list(inh_down.get(cls_cid, []))
        seen = {cls_cid}
        while q_:
            c = q_.pop(0)
            if c in seen:
                continue
            seen.add(c)
            out.append(c)
            q_.extend(inh_down.get(c, []))
        return out

    inheritance = None
    inherited_sources = []   # (ancestor method entity, hops) for knowledge walk
    if e.kind == "class":
        inheritance = {
            "bases": [ix.entities[c].qualname for c in inh_up.get(e.coord_id, [])
                      if c in ix.entities],
            "subclasses": [ix.entities[c].qualname
                           for c in _descendants(e.coord_id)
                           if c in ix.entities],
        }
    elif e.kind == "method" and e.parent and e.parent in ix.entities:
        cls_cid = e.parent
        short = e.qualname.rsplit(".", 1)[-1].split("#")[0]
        overrides, overridden_by, inherits_to = None, [], []
        for hops, anc in enumerate(_mro(cls_cid), start=1):
            anc_e = ix.entities.get(anc)
            if not anc_e:
                continue
            base_m = ix.resolve(f"{anc_e.qualname}.{short}")
            if base_m:
                if overrides is None:
                    overrides = base_m.qualname
                inherited_sources.append((base_m, hops))
        for sub in _descendants(cls_cid):
            sub_e = ix.entities.get(sub)
            if not sub_e:
                continue
            sub_m = ix.resolve(f"{sub_e.qualname}.{short}")
            if sub_m:
                overridden_by.append(sub_e.qualname)
            else:
                inherits_to.append(sub_e.qualname)
        inheritance = {
            "defined_on": ix.entities[cls_cid].qualname,
            "overrides": overrides,
            "overridden_by": overridden_by,
            "inherited_unchanged_by": inherits_to,
        }

    # ---- comments: the line-level "why", with rot detection ----
    import re as _re
    _markers = []
    for c in getattr(e, "comments", []) or []:
        mk = _re.match(r"(TODO|FIXME|HACK|XXX|NOTE|WARNING)\b[:\s]*(.*)",
                       c["text"], _re.I)
        if mk:
            _markers.append({"tag": mk.group(1).upper(), "line": c["line"],
                             "text": mk.group(2)[:120]})

    # Check for comment rot, but suppress if confirmed at current logic_hash
    has_rot = bool(getattr(e, "comment_rot", False))
    if has_rot:
        # Read meta to check for non-stale confirm entry (stamp matches current logic_hash)
        _confirm_meta = meta.read_all(e.coord_id,
                                       current_hash=accepted_for(e))
        _confirm_entries = _confirm_meta.get("confirm", [])
        if any(not en.get("stale") for en in _confirm_entries):
            has_rot = False  # Confirmed at current logic_hash, suppress rot

    comments_block = {
        "total": len(getattr(e, "comments", []) or []),
        "markers": _markers,
        "rot": has_rot,
    }

    # ---- design docs governing this coordinate ----
    from .harvest import harvest_docs
    _bindings = harvest_docs(repo_p, ix, coord,
                             write=not _READ_ONLY)
    design_docs = []
    for _doc, _b in _bindings.items():
        _ref = _b.get("refs", {}).get(e.coord_id)
        if _ref:
            _cur = getattr(e, "logic_hash", "") or e.body_hash
            design_docs.append({
                "doc": _doc,
                "status": "fresh" if _ref["logic_hash"] == _cur
                          else "entity-changed-since-doc"})

    md = meta.read_all(e.coord_id,
                       current_hash=accepted_for(e))
    knowledge, has_stale = [], False
    for channel, entries in md.items():
        for en in entries:
            stale = bool(en.get("stale"))
            has_stale = has_stale or stale
            knowledge.append({"channel": channel, "text": en["text"],
                              "stale": stale})
    # knowledge flows DOWN the hierarchy: a note on the ancestor method
    # this one overrides (or inherits from) describes behavior this
    # entity shares - surface it with provenance, staleness checked
    # against the ANCESTOR's body.
    for base_m, hops in inherited_sources:
        md_i = meta.read_all(
            base_m.coord_id,
            current_hash=accepted_for(base_m))
        for channel, entries in md_i.items():
            for en in entries:
                knowledge.append({
                    "channel": channel, "text": en["text"],
                    "stale": bool(en.get("stale")),
                    "inherited_from": base_m.qualname,
                    "hops_up": hops})

    from . import evidence as _ev
    _records = _ev.read(coord, e.coord_id)
    _ev.decorate_knowledge(knowledge, _records)
    _evidence = _ev.summarise(_records) if _records else None

    recent = VersionStore(coord).ancestry(e.coord_id)[-3:]

    warnings = []
    cx = m.get("complexity", 0)
    if cx >= 15:
        warnings.append(f"HIGH COMPLEXITY ({cx}): this is among the "
                        "hardest code in the repo - small edits have "
                        "outsized bug risk")
    if len(callers) >= 5:
        warnings.append(f"WIDELY DEPENDED ON ({len(callers)} direct "
                        "callers): signature/behavior changes ripple")
    if radius["is_lower_bound"]:
        warnings.append("DOWNSTREAM REACH IS A LOWER BOUND: dynamic "
                        "event emission reached - real impact may "
                        "exceed what the graph shows")
    if has_stale:
        warnings.append("STALE KNOWLEDGE ATTACHED: notes below were "
                        "written against an older body - verify "
                        "before trusting")
    if comments_block["rot"]:
        warnings.append("COMMENT ROT: comments unchanged across a "
                        "behavior change - they may describe old logic. "
                        "Workflow: draft updated comment(s) reflecting the "
                        "new behavior and propose the edit for user "
                        "approval; applying it clears this flag on the "
                        "next re-index")
    for _d in design_docs:
        if _d["status"] != "fresh":
            warnings.append(f"GOVERNED BY {_d['doc']}: this entity's "
                            "logic changed since the doc last touched "
                            "it - the doc may be outdated (advisory)")
    if inheritance and inheritance.get("overridden_by"):
        n_ov = len(inheritance["overridden_by"])
        warnings.append(f"OVERRIDDEN IN {n_ov} SUBCLASS(ES): a change "
                        "here may be shielded by (or diverge from) "
                        "overrides - verify dispatch paths")
    if any(r["kind"] in ("renamed", "moved") for r in recent):
        warnings.append("RECENTLY RENAMED/MOVED: identity changed in "
                        "recent versions - grep for old names may "
                        "mislead teammates")

    return {
        "entity": _entity_dict(e),
        "grounding": grounding,
        "inheritance": inheritance,
        "comments": comments_block,
        "design_docs": design_docs,
        "metrics": {"complexity": cx,
                    "fan_in": m.get("fan_in", 0),
                    "churn": m.get("churn", 0)},
        "direct_callers": callers,
        "downstream": radius,
        "knowledge": knowledge,
        **({"evidence": _evidence} if _evidence else {}),
        "recent_history": [{"kind": r["kind"], "note": r.get("note", "")}
                           for r in recent],
        "warnings": warnings,
    }


def at(repo: str, location: str) -> dict:
    """The grep handoff: 'path/to/file.py:123' -> the innermost entity
    containing that line, plus its enclosing chain. Grep speaks
    locations; the map speaks names - this is the weld between them."""
    ctx = _ctx(repo)
    if not ctx:
        return {"error": f"no index at {repo}; run memway init first"}
    repo_p, coord, ix, edges, meta = ctx
    if ":" not in location:
        return {"error": "expected file:line, e.g. src/app.py:42"}
    path, _, line_s = location.rpartition(":")
    try:
        line = int(line_s)
    except ValueError:
        return {"error": f"line must be an integer, got {line_s!r}"}
    path = path.lstrip("./")
    hits = [e for e in ix.entities.values()
            if e.path == path and e.kind != "module"
            and e.lineno <= line <= (e.end_lineno or e.lineno)]
    if not hits:
        mods = [e for e in ix.entities.values()
                if e.path == path and e.kind == "module"]
        if mods:
            return {"location": location,
                    "entity": _entity_dict(mods[0]),
                    "note": "line falls between entities; module scope"}
        return {"error": f"no indexed file matches {path!r} "
                "(paths are repo-relative)"}
    inner = min(hits, key=lambda e: (e.end_lineno or e.lineno) - e.lineno)
    chain = sorted(hits,
                   key=lambda e: (e.end_lineno or e.lineno) - e.lineno,
                   reverse=True)
    return {"location": location,
            "entity": _entity_dict(inner, meta),
            "enclosing": [h.qualname for h in chain if h is not inner]}


# router used by both the --json CLI path and the MCP server
QUERIES = {
    "show": lambda repo, a: show(repo, a[0]),
    "lineage": lambda repo, a: lineage(repo, a[0]),
    "at": lambda repo, a: at(repo, a[0]),
    "summary": lambda repo, a: summary(repo),
    "before-edit": lambda repo, a: before_edit(repo, a[0]),
    # UNCAPPED on purpose: a file on disk has no context window, and the
    # MCP path is the only one that needs a byte ceiling (see dig.py).
    "dig": lambda repo, a: _dig(repo, a[0]),
    # Same function the MCP tool calls, never a second implementation -
    # two answers to "what did I just break" is worse than none.
    #
    # `run` is pinned False and takes no argument on this surface. Reporting
    # which tests reach a change is a read; executing them is not, and a
    # query that shells out to pytest is a different tool than this one.
    #
    # THE ODD ONE OUT: every other query leaves .coord untouched, and this
    # one does not - it re-indexes and rewrites the edge cache so the map
    # reflects the tree it just measured (see verify_change below). That is
    # the MCP tool's long-standing behaviour and is shared deliberately, but
    # it means `--json verify-change` is a WRITE. test_verify_query records
    # that so nobody infers inertness from the company it keeps.
    "verify-change": lambda repo, a: verify_change(repo),
}


def _dig(repo, ref):
    from .dig import dig as _d
    return _d(repo, ref)


def verify_change(repo_root, run=False):
    """MCP/CLI entry: post-change impact + test selection (see verify.py)."""
    from pathlib import Path
    from .indexer import Indexer
    from .edges import EdgeBuilder
    from .verify import verify_change as _vc
    repo_root = Path(repo_root)
    ix = Indexer(repo_root, repo_root / ".coord")
    ix.load_existing()
    eb = EdgeBuilder(ix)
    edges = EdgeBuilder.load(repo_root / ".coord") or []
    result = _vc(ix, edges or eb.build(), repo_root, run=run)
    # refresh edges against the new tree so the next call sees them
    eb2 = EdgeBuilder(ix); eb2.build(); eb2.save(repo_root / ".coord")
    ix.save()
    return result


def probe(repo_root, ref, args=None, kwargs=None, setup="", record=False):
    """MCP/CLI entry: run an entity with values, return the flow as
    coordinates (see probe.py)."""
    from pathlib import Path
    from .indexer import Indexer
    from .edges import EdgeBuilder
    from .probe import probe as _probe
    repo_root = Path(repo_root)
    ix = Indexer(repo_root, repo_root / ".coord")
    ix.load_existing()
    if not ix.entities:
        ix.index(); ix.save()
    edges = EdgeBuilder.load(repo_root / ".coord")
    if not edges:
        eb = EdgeBuilder(ix); edges = eb.build(); eb.save(repo_root / ".coord")
    return _probe(ix, edges, repo_root, ref, args, kwargs, setup, record)


def agent_meta(repo_root, ref, channel, text, author="agent"):
    """MCP entry: agent write-back. Attach an observation to a coordinate.

    The entry is body-hash-stamped at write time (D10), so if the code
    later changes, the note is flagged stale rather than silently lying.
    Author attribution keeps agent notes distinguishable from human ones.
    """
    ctx = _ctx(repo_root)
    if not ctx:
        return {"error": f"no index at {repo_root}; run memway init first"}
    repo_p, coord, ix, edges, meta = ctx
    e = ix.resolve(ref)
    if not e:
        # deliberately a POINTER, not a redirect: reads may be served from
        # the successor, but silently writing knowledge to a coordinate the
        # caller did not name is the kind of surprise that erodes trust in
        # the store. Caller re-issues against superseded_by.
        return _resolve_error(ref, ix, coord)
    # stamp with logic_hash: the note survives comment/docstring edits and
    # flags stale only when BEHAVIOR changes (falls back to body hash)
    meta.add(e.coord_id, channel, text, author=author,
             body_hash=stamp_for(e))
    return {
        "attached": {"coord": e.coord_id, "qualname": e.qualname,
                     "channel": channel, "author": author},
        "note": "entry is body-hash-stamped; it will be flagged stale if "
                "the entity's body changes",
    }


def attention(repo_root, limit=20):
    """MCP entry: the repo's attention queue - everything currently
    flagged as possibly wrong or needing eyes, in one call.

    Aggregates: comment rot (comments unchanged across behavior
    changes), TODO/FIXME/HACK markers, design docs whose governed
    entities drifted, stale knowledge notes, and pending-review lineage
    links. The repo-wide complement to the per-entity briefing.
    """
    import re as _re
    ctx = _ctx(repo_root)
    if not ctx:
        return {"error": f"no index at {repo_root}; run memway init first"}
    repo_p, coord, ix, edges, meta = ctx

    # Comment rot, but suppress entries confirmed at current logic_hash
    rot = []
    for e in ix.entities.values():
        if getattr(e, "comment_rot", False):
            # Check for non-stale confirm entry
            _confirm_meta = meta.read_all(e.coord_id,
                                           current_hash=accepted_for(e))
            _confirm_entries = _confirm_meta.get("confirm", [])
            if not any(not en.get("stale") for en in _confirm_entries):
                # No current confirmation, include in rot list
                rot.append(e.qualname)
    rot = rot[:limit]

    markers = []
    for e in ix.entities.values():
        for c in getattr(e, "comments", []) or []:
            mk = _re.match(r"(TODO|FIXME|HACK|XXX)\b[:\s]*(.*)",
                           c["text"], _re.I)
            if mk:
                markers.append({"tag": mk.group(1).upper(),
                                "entity": e.qualname,
                                "text": mk.group(2)[:100]})
    markers.sort(key=lambda x: ("FIXME", "HACK", "XXX", "TODO"
                                ).index(x["tag"]))

    from .harvest import harvest_docs
    stale_docs = []
    for doc, b in harvest_docs(repo_p, ix, coord,
                               write=not _READ_ONLY).items():
        drifted = []
        for cid, ref in b.get("refs", {}).items():
            e = ix.entities.get(cid)
            if e is not None:
                cur = getattr(e, "logic_hash", "") or e.body_hash
                if cur != ref["logic_hash"]:
                    drifted.append(ref["qualname"])
        if drifted:
            stale_docs.append({"doc": doc, "drifted_entities": drifted})

    stale_notes = 0
    for e in ix.entities.values():
        md = meta.read_all(e.coord_id, current_hash={
            getattr(e, "logic_hash", ""), e.body_hash})
        stale_notes += sum(1 for ens in md.values()
                           for en in ens if en.get("stale"))

    return {
        "comment_rot": rot,
        "markers": markers[:limit],
        "marker_total": len(markers),
        "stale_design_docs": stale_docs,
        "stale_notes": stale_notes,
        "note": "each item is a place where recorded intent and current "
                "behavior may disagree. For comment_rot: draft updated "
                "comments reflecting the new behavior and propose the "
                "edits for user approval - an applied edit clears the "
                "flag on re-index. For other items: verify, then confirm "
                "or update",
    }


READ_CAP_BYTES = 60_000


def apply_read_cap(payload: dict, cap: int = READ_CAP_BYTES) -> dict:
    """Trim a briefing to a byte ceiling, DERIVED first.

    Order is the contract, not an optimisation. Evidence is regenerable
    with one re-dig; authored knowledge is somebody's judgment and is
    gone forever if it is dropped. So evidence trims - bodies, then
    items, then the whole section - before a single authored entry is
    touched, and every cut is declared.
    """
    import json as _j

    def size(p):
        return len(_j.dumps(p, default=str).encode())

    if size(payload) <= cap:
        return payload
    cuts = []
    ev = payload.get("evidence")
    if ev and ev.get("top"):
        while len(ev["top"]) > 1 and size(payload) > cap:
            ev["top"].pop()
        ev["truncated"] = True
        cuts.append("evidence items")
    if size(payload) > cap and "evidence" in payload:
        payload.pop("evidence")
        cuts.append("evidence section (regenerable: re-dig)")
    if size(payload) > cap:
        # only now, and loudly: authored knowledge is irreplaceable
        kn = payload.get("knowledge", [])
        while len(kn) > 1 and size(payload) > cap:
            kn.pop()
        cuts.append("AUTHORED KNOWLEDGE - irreplaceable, and still over cap")
    payload["payload_capped"] = {
        "cap_bytes": cap, "trimmed": cuts,
        "note": "derived evidence is sacrificed before authored knowledge; "
                "use the CLI for the uncapped payload",
    }
    return payload
