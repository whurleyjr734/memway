"""Structured query API: the map's facts as data, not formatted text.

Both `coordsys <cmd> --json` and the MCP server call these functions.
The CLI's human-readable printers are untouched; this module is the
one structured surface, so agents and humans never drift apart.

Every function takes a loaded context and returns JSON-serializable
dicts/lists. Errors are returned as {"error": "..."} rather than
raised, so an agent gets a usable message instead of a stack trace.

Context loading is shared via _ctx(); edge grounding (resolution provenance
and confidence) is surfaced in before_edit to calibrate trust in static analysis.
"""

from __future__ import annotations

from pathlib import Path

from .indexer import Indexer
from .edges import EdgeBuilder, neighbors
from .metadata import MetaStore
from .metrics import MetricsStore
from .lineage import VersionStore


def _ctx(repo: str):
    """Load the map once; shared by every query."""
    repo_p = Path(repo).resolve()
    coord = repo_p / ".coord"
    if not (coord / "index" / "coordinates.json").exists():
        return None
    ix = Indexer(repo_p, coord)
    ix.load_existing()
    ix.load_raw_edges()
    edges = EdgeBuilder.load(coord)
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
                       current_hash={getattr(e, "logic_hash", ""), e.body_hash})
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
    return d


# --------------------------------------------------------------- queries

def show(repo: str, ref: str) -> dict:
    ctx = _ctx(repo)
    if not ctx:
        return {"error": f"no index at {repo}; run coordsys init first"}
    _, _, ix, edges, meta = ctx
    e = ix.resolve(ref)
    if not e:
        return {"error": f"no entity matches {ref!r}"}
    out = _entity_dict(e, meta)
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
        })
    out["edges"] = rel
    return out


def lineage(repo: str, ref: str) -> dict:
    ctx = _ctx(repo)
    if not ctx:
        return {"error": f"no index at {repo}; run coordsys init first"}
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
        return {"error": f"no index at {repo}; run coordsys init first"}
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
    prod = [(ms.data.get(cid, {}).get("complexity", 0),
             e.qualname)
            for cid, e in ix.entities.items()
            if e.kind in ("function", "method")
            and "test" not in e.path.lower()]
    prod.sort(reverse=True)

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
        "hardest": [{"qualname": q, "complexity": c}
                    for c, q in prod[:5]],
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
        return {"error": f"no index at {repo}; run coordsys init first"}
    repo_p, coord, ix, edges, meta = ctx
    e = ix.resolve(ref)
    if not e:
        return {"error": f"no entity matches {ref!r}"}

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
                                       current_hash={getattr(e, "logic_hash", ""), e.body_hash})
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
    _bindings = harvest_docs(repo_p, ix, coord)
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
                       current_hash={getattr(e, "logic_hash", ""), e.body_hash})
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
            current_hash={getattr(base_m, "logic_hash", ""), base_m.body_hash})
        for channel, entries in md_i.items():
            for en in entries:
                knowledge.append({
                    "channel": channel, "text": en["text"],
                    "stale": bool(en.get("stale")),
                    "inherited_from": base_m.qualname,
                    "hops_up": hops})

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
        return {"error": f"no index at {repo}; run coordsys init first"}
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
}


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
        return {"error": f"no index at {repo_root}; run coordsys init first"}
    repo_p, coord, ix, edges, meta = ctx
    e = ix.resolve(ref)
    if not e:
        return {"error": f"no entity matches {ref!r}"}
    # stamp with logic_hash: the note survives comment/docstring edits and
    # flags stale only when BEHAVIOR changes (falls back to body hash)
    meta.add(e.coord_id, channel, text, author=author,
             body_hash=getattr(e, "logic_hash", "") or e.body_hash)
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
        return {"error": f"no index at {repo_root}; run coordsys init first"}
    repo_p, coord, ix, edges, meta = ctx

    # Comment rot, but suppress entries confirmed at current logic_hash
    rot = []
    for e in ix.entities.values():
        if getattr(e, "comment_rot", False):
            # Check for non-stale confirm entry
            _confirm_meta = meta.read_all(e.coord_id,
                                           current_hash={getattr(e, "logic_hash", ""), e.body_hash})
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
    for doc, b in harvest_docs(repo_p, ix, coord).items():
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
