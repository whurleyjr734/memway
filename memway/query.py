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
from .metadata import (MetaStore, stamp_for, accepted_for, for_display,
                       unsuperseded_stale, rot_is_answered)
from .metrics import MetricsStore
from .payload import rank_bound_report

# Below this, an edge is a GUESS rather than a resolved reference. Named
# because two surfaces now ask the question - the grounding block and the
# widely-depended-on warning - and a second literal is how they drift.
LOW_CONFIDENCE = 0.7
from .lineage import VersionStore
from . import refs


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


def _short(name: str) -> str:
    """Last segment of a reference, stripped of disambiguators.

    Delegates. This spelled the strip itself in 0.55.5 - `re.split(r"[/#]",
    ...)` - which was a fourth module deciding what a suffix looks like,
    the same shape as the defect 0.56.0 exists to end. refs is the only
    module that knows, on the reading side as well as the writing side.
    """
    return refs.short_of(name)


def _unresolved_refs_to(ix, edges, ent) -> int:
    """How many emitted call references name this entity but produced no
    edge. 0 when everything that could point here did.

    Deliberately generous on the input side and strict on the output
    side: any raw call whose bare target matches this entity's bare name
    is a candidate, and every resolved incoming call edge cancels one.
    The remainder is what the map cannot account for. Over-counting here
    is safe - it can only turn a confident number into an admittedly
    incomplete one, never the reverse.

    WHAT IT CANNOT SEE, and this is not a defect to be fixed here: a
    disambiguator refs does not know. To notice that an emitted `helper`
    should have reached an entity registered as `helper@1`, something
    must already know that `@` introduces a suffix - which is exactly the
    knowledge refs.py is the sole holder of. The guarantee against that
    class is refs being the ONLY producer of a reference, not this
    counter catching the drift afterwards. Asserted in
    tests/test_never_silent.py so the limit cannot quietly be forgotten
    and this cannot be sold as a general desync detector.

    Since 0.56.0 reconciled /arity and #N it fires rarely by design; it
    stays as a cheap floor, not as the mechanism.
    """
    raw = getattr(ix, "_raw_edges", None) or []
    if not raw:
        return 0
    # ZERO CANDIDATES IS A BLIND SPOT. Several candidates is a REFUSAL,
    # and refusing to guess is the entire point of the 0.54.3 guards.
    #
    # This asked `resolve(ref) is None`, and resolve returns None for both
    # - so an ambiguous name counted as a gap in the map. The comment here
    # claimed "the loop already knows the difference"; it did not. Found on
    # SQLAlchemy, where `execute` names 41 entities and before_edit
    # announced "3294 call references could not be resolved to any entity"
    # about a name the resolver was correctly declining. Every repo pinned
    # in the corpus floors is small enough that ambiguity at that scale
    # never appeared.
    #
    # Second time this counter has confused a decision for a gap: 0.55.5
    # shipped with a guard against it, and 0.56.0 removed the guard on the
    # reasoning quoted above. Asking candidates() answers the question
    # directly instead of inferring it from a None.
    target = _short(ent.qualname)
    # The entity's name is spelled one way in the index and another in the
    # emitted reference, so the resolver never had a candidate to judge.
    # That is not a decision; it is a blind spot, and it is the one thing
    # this counter exists to surface.
    seen: dict = {}
    n = 0
    for r in raw:
        if getattr(r, "kind", None) != "calls":
            continue
        ref = getattr(r, "dst_ref", "")
        if _short(ref) != target:
            continue
        if ref not in seen:
            seen[ref] = not ix.candidates(ref)
        if seen[ref]:
            n += 1
    return n


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
        md = meta.read_all(e.coord_id, current_hash=accepted_for(e))
        knowledge = [{
            "channel": r["channel"],
            "text": r["text"],
            "stale": bool(r.get("stale")),
            "superseded": r["superseded"],
            "author": r.get("author", ""),
        } for r in for_display(md)]
        # THE DECIDING ENTRY FIRST, HISTORY BEHIND IT, AND BOUNDED.
        # Knowledge is append-only and never deleted - authored content is
        # precious - so a well-used coordinate accumulates. before_edit on
        # this repo's own before_edit shipped 12 entries, 10,587 of the
        # payload's 14,673 characters, and ELEVEN of the twelve were
        # superseded: history, not warnings.
        #
        # Superseded means somebody already answered it (metadata.
        # for_display), so it is exactly the right thing to truncate -
        # nothing is lost, it is still on disk and `memway show` reads it.
        # The unsuperseded entry per channel is the one that decides, and
        # it is never cut.
        knowledge, _kn_report = rank_bound_report(
            knowledge, "knowledge", rank=lambda r: r["superseded"], cap=6)
        d["knowledge"] = knowledge
        d.update(_kn_report)
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

    all_qualnames = list(ix.by_qualname.keys())

    # AMBIGUITY IS NOT ABSENCE. A bare name matching several entities used
    # to be reported as "no entity matches", which is false and sends the
    # caller down the wrong path: they conclude the map does not know the
    # thing and fall back to grep, when the map knows five and needed one
    # word of disambiguation. Measured in the 0.54.0 acceptance -
    # `get_signature` matched 5 entities in itsdangerous, `save` matched 3
    # here, and both answered "no entity matches". The candidates are in
    # hand at the moment of failure; only the message was wrong.
    # THROUGH candidates(), not a second copy of the rule. This asked
    # `qn.rsplit(".", 1)[-1] == tail`, a RAW last segment, which cannot see
    # a disambiguated registration: `handle` never matched `handle#2` and
    # `separateCamelCase` never matched `separateCamelCase/2`. So the third
    # surface of the refusal-vs-absence bug outlived the two that were
    # fixed - on sqlalchemy this said "29 entities match" where the
    # resolver had 41, and on a repo with two same-named functions it said
    # "no entity matches" while printing both under "closest:".
    # refs.short_of is the only thing that knows what a suffix looks like,
    # and candidates() is the only thing that applies it.
    matches = sorted(ix.entities[cid].qualname for cid in ix.candidates(ref)
                     if cid in ix.entities)
    if str(ref) in ix.by_qualname and str(ref) not in matches:
        matches = sorted(matches + [str(ref)])
    if len(matches) > 1:
        return {
            "error": f"{ref!r} is ambiguous - {len(matches)} entities match",
            "matches": matches,
            "hint": (f"qualify it, e.g. {matches[0]!r}, or use "
                     f"memway_at <file:line>"),
        }

    # Find fuzzy matches
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
    # RANKED, BOUNDED, REPORTED - through the one function that does it.
    # This shipped every edge: `show` on DirLocker.Lock in prometheus came
    # to 55,829 characters, of which 55,572 was this list (344 entries).
    #
    # Ordering is the half that makes the cut survivable. OUT edges first
    # - what this entity calls describes the entity, and there are always
    # far fewer of them than callers - then production before tests, then
    # confidence, so a bare-name guess never displaces an exact edge.
    def _edge_rank(r):
        te = ix.by_qualname.get(r["target"])
        return (0 if r["direction"] == "out" else 1,
                1 if te and is_test_entity(ix.entities[te]) else 0,
                -float(r.get("confidence") or 0),
                str(r["target"]))

    from .verify import is_test_entity
    rel_shown, rel_report = rank_bound_report(rel, "edges", rank=_edge_rank)
    out["edges"] = rel_shown
    out.update(rel_report)
    out["map_lag"] = _map_lag(repo, coord)
    out["knowledge_lag"] = _knowledge_lag(ix, meta)
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


def _map_lag(repo, coord) -> dict:
    """The freshness gap, for read surfaces. {} when there is none.

    THE GUARANTEE. Hooks cover commit, checkout and merge; they cannot
    fire during a bisect, in a fresh worktree, on a hand-edited tree, or
    anywhere nobody ran `memway hooks install`. So every read tool asks
    this, and a lagging map says so on the way past. The map may lag; it
    must never lag SILENTLY.

    Read-only by construction: reads the manifest, asks git two questions.
    Nothing here writes, which is what keeps it inside the read fence.
    """
    from .freshness import lag
    try:
        return lag(repo, coord)
    except Exception:
        return {}          # a broken freshness check must not break a read


def _knowledge_lag(ix, meta) -> dict:
    """Coordinates holding stale knowledge nobody has answered. {} if none.

    THE SAME GUARANTEE AS _map_lag, applied to the other thing that rots.
    freshness.py wrote the principle down for maps - "the map may lag; it
    must never lag SILENTLY" - and enforced it by making every read say so
    on the way past. Knowledge got the detection and none of the telling:
    `show <ref>` flagged a stale entry only if you already suspected that
    coordinate, and nothing said anything repo-wide.

    Which is exactly how it failed. 0.54.1 shipped a workflow rule saying
    "supersede what your change staled" and then broke it within the hour,
    twice in one evening, by the person who wrote the rule with the tool
    installed. Nothing told them the six coordinates existed. A rule that
    depends on recall is the failure mode this project exists to fix.

    SUPERSEDED HISTORY NEVER COUNTS. This asks unsuperseded_stale - the
    ring's rule, newest-per-channel - so a repo that has answered every
    stale entry reads silent even though the superseded text is still on
    disk. The flagship holds 23 individually-stale entries and must report
    nothing. A warning that fires forever is not a warning.

    Read-only by construction: entities already in memory, plus the meta
    files. Nothing here writes, which is what keeps it inside the fence.
    """
    try:
        coords = []
        for cid, e in ix.entities.items():
            rows = for_display(meta.read_all(cid, accepted_for(e)))
            if unsuperseded_stale(rows):
                coords.append(cid)
        if not coords:
            return {}
        n = len(coords)
        return {
            "coordinates": sorted(coords),
            "count": n,
            "message": (f"{n} coordinate{'s' if n != 1 else ''} hold"
                        f"{'' if n != 1 else 's'} stale knowledge "
                        f"- memway attention"),
        }
    except Exception:
        return {}          # a broken staleness check must not break a read


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

    # THE NUMBER AND ITS PROVENANCE, SIDE BY SIDE. `hardest` says what is
    # complicated; this says what is depended upon - and how much of that
    # dependence the resolver actually resolved.
    #
    # It exists because the same manual move found a real defect on three
    # consecutive unfamiliar repos: index it, sort by call in-degree, read
    # the top few. prometheus put a 3-line `len` method at 1,619 callers,
    # scikit-learn put a pretty-printer's `format` at 292, django put a GIS
    # mixin's `append` at 573. Two were confidently wrong; the third was an
    # honest guess reported as a fact.
    #
    # DELIBERATELY NOT A DETECTOR. No threshold, no warning, no flag - a
    # heuristic on a heuristic would need a number chosen to make the three
    # known cases light up, and would then cry wolf on the genuinely hot
    # utilities that sit beside them (django's assertRaisesMessage has
    # 1,935 callers and every one is real). This asserts nothing. It puts
    # the count next to how it was resolved and lets the reader do what a
    # reader is for. Same posture as dig: return candidates, never judge.
    _indeg: dict = {}
    _guessed: dict = {}
    for _e in edges:
        if _e.get("kind") != "calls":
            continue
        d = _e.get("dst")
        _indeg[d] = _indeg.get(d, 0) + 1
        if float(_e.get("confidence", 1.0)) < LOW_CONFIDENCE:
            _guessed[d] = _guessed.get(d, 0) + 1
    _dep = []
    for cid, n in _indeg.items():
        ent = ix.entities.get(cid)
        if not ent:
            continue
        _dep.append({"qualname": ent.qualname, "callers": n,
                     "guessed": _guessed.get(cid, 0),
                     "is_test": is_test_entity(ent)})
    _dep_shown, _dep_report = rank_bound_report(
        _dep, "most_depended_on", rank=lambda d: (-d["callers"], d["qualname"]),
        cap=5)

    _hardest_shown, _hardest_report = rank_bound_report(
        [{"qualname": q, "complexity": c, "is_test": t}
         for c, q, t in prod], "hardest", cap=5)
    _hardest_all_shown, _hardest_all_report = rank_bound_report(
        [{"qualname": q, "complexity": c, "is_test": t}
         for c, q, t in ranked], "hardest_overall", cap=5)
    _entries_shown, _entries_report = rank_bound_report(
        know, "entries")
    return {
        "map_lag": _map_lag(repo, coord),
        "knowledge_lag": _knowledge_lag(ix, meta),
        "entities": len(ix.entities),
        "edges": len(edges),
        "languages": dict(langs),
        "kinds": dict(kinds),
        # `hardest` keeps its meaning exactly - source only - because
        # consumers already depend on it. is_test rides along so every
        # entry in both lists has the same shape. `hardest_overall` is
        # the new, additive view: the same numbers, nothing excluded.
        # Top-five lists, but they SAY they are top-five now. Both were
        # silent slices: a reader saw five and could not tell whether the
        # repo had five or five hundred.
        "most_depended_on": _dep_shown,
        **_dep_report,
        "hardest": _hardest_shown,
        **_hardest_report,
        "hardest_overall": _hardest_all_shown,
        **_hardest_all_report,
        "entities_by_origin": {"source": n_src,
                               "tests": len(ix.entities) - n_src},
        "knowledge": {
            "total_entries": total_entries,
            "coordinates_with_knowledge": len(know),
            "by_channel": dict(chan_counts),
            "superseded": superseded_count,
            "entries": _entries_shown,
            **_entries_report,
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
                                "path": src.path, "line": src.lineno,
                                "_conf": float(edge.get("confidence", 1.0))})

    # RANKED AND BOUNDED, AND IT SAYS SO. This listed every caller, which
    # on a hot entity is not a briefing but a dump: DirLocker.Lock in
    # prometheus returned 342 of them and the whole payload came to 53,534
    # characters - roughly 13k tokens for one pre-edit check, most of it
    # test callers nobody asked about.
    #
    # The reader almost always wants the same thing: who depends on this
    # that MATTERS. So production before tests, then the caller's own
    # fan-in (a caller that many things use tells you more than a leaf),
    # then qualname so the order is stable across runs.
    #
    # THE TRUNCATION IS VISIBLE. This map's own guard message says nothing
    # is ever sampled silently, and a list that quietly stops at 12 is a
    # sampled list - so the counts ride alongside and the caller can ask
    # for the rest with `memway show`.
    from .verify import is_test_entity
    _fan = {}
    for edge in edges:
        if edge["kind"] == "calls":
            _fan[edge["dst"]] = _fan.get(edge["dst"], 0) + 1
    _by_q = {c["qualname"]: c for c in callers}

    def _rank(c):
        ent = ix.by_qualname.get(c["qualname"])
        return (1 if is_test_entity(ix.entities[ent]) else 0,
                -_fan.get(ent, 0), c["qualname"]) if ent else (2, 0, c["qualname"])

    callers_tests = sum(1 for c in callers
                        if (q := ix.by_qualname.get(c["qualname"]))
                        and is_test_entity(ix.entities[q]))
    # HOW MANY OF THOSE CALLERS ARE GUESSES. Measured on django@cccc004:
    # ListMixin.append has 573 direct callers and ALL 573 are bare-name
    # guesses at confidence 0.6 - ordinary `results.append(x)` across the
    # codebase landing on a GIS mixin. The resolver is behaving correctly
    # there; it could not type the receiver and refused to claim
    # certainty. What was wrong is the SENTENCE built from that number.
    callers_guessed = sum(1 for c in callers
                          if c["_conf"] < LOW_CONFIDENCE)
    callers_shown, callers_report = rank_bound_report(
        callers, "direct_callers", rank=_rank)
    for c in callers_shown:
        c.pop("_conf", None)

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
    # UNRESOLVED REFERENCES MAKE THE COUNT A LOWER BOUND, and until 0.55.5
    # only dynamic event emission could say so. That left the worst case
    # silent: on google/gson, 0 of 1770 resolved call edges reach a method,
    # because JavaParser emits a BARE name while its own overload
    # disambiguation registers every method as Type.method/arity - so every
    # Java method read "callers 0, blast 0" with is_lower_bound FALSE, an
    # affirmative claim that the zero was complete. The same shape costs
    # Python three edges on @overload stubs (#N instead of /arity).
    #
    # This does not fix resolution; it stops the report lying about it.
    # If the parser emitted a call to this entity's bare name and the
    # resolver produced no edge for it, then the caller list is a floor,
    # whatever the language and whatever the reason. Computed from data
    # already on disk, so it costs a set membership test.
    unresolved = _unresolved_refs_to(ix, edges, e)
    radius = {
        "downstream_count": len(affected),
        # DEPTH 1 IS `direct_callers`, ALREADY ABOVE. This repeated all
        # 342 of them as bare qualnames on prometheus - 15,947 characters
        # of pure duplication, 100% overlap, measured. The count stays
        # because the shape of the radius is the point here; the names
        # live in one place.
        "direct_count": sum(1 for a in affected if a["depth"] == 1),
        "transitive_count": sum(1 for a in affected if a["depth"] > 1),
        "via_event": [a["qualname"] for a in affected if a["via_event"]],
        "is_lower_bound": bool(b.get("radius_is_lower_bound", False)
                               or unresolved),
        "unresolved_refs": unresolved,
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
        short = refs.short_of(e.qualname)
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
        # BOUNDED LIKE EVERY OTHER LIST. The 0.58.0 census measured
        # `before_edit` on a Go entity with no inheritance at all, so it
        # never saw this one: on django@cccc004,
        # SimpleTestCase.assertRaisesMessage is inherited unchanged by
        # 2,389 test classes and this single field was 133,163 of the
        # briefing's 136,453 characters - 97.6%. A census is only as good
        # as the entity it is run on.
        #
        # `overrides` and `defined_on` are single values and stay whole.
        ov_shown, ov_report = rank_bound_report(
            sorted(overridden_by), "overridden_by")
        inh_shown, inh_report = rank_bound_report(
            sorted(inherits_to), "inherited_unchanged_by")
        inheritance = {
            "defined_on": ix.entities[cls_cid].qualname,
            "overrides": overrides,
            "overridden_by": ov_shown,
            **ov_report,
            "inherited_unchanged_by": inh_shown,
            **inh_report,
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

    # Comment rot, suppressed by a CURRENT confirm - the one rule, asked.
    # This was the third hand-written copy of it (attention had one, and
    # verify_change was about to add a fourth); the structural pin in
    # tests/test_read_fence.py found this one the moment it existed.
    has_rot = bool(getattr(e, "comment_rot", False))
    if has_rot and rot_is_answered(
            meta.read_all(e.coord_id, current_hash=accepted_for(e))):
        has_rot = False

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
    for r in for_display(md):
        stale = bool(r.get("stale"))
        has_stale = has_stale or (stale and not r["superseded"])
        knowledge.append({"channel": r["channel"], "text": r["text"],
                          "stale": stale, "superseded": r["superseded"]})
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
        # THE COUNT AND ITS CONFIDENCE ARE ONE SENTENCE, not two. This
        # said "WIDELY DEPENDED ON (573 direct callers)" as a headline
        # while the fact that every one of those 573 was a name guess sat
        # in a separate grounding block, phrased about the whole radius
        # rather than about this number. A reader gets the claim and the
        # caveat in different places and weighs the claim.
        if callers_guessed == len(callers):
            warnings.append(
                f"WIDELY DEPENDED ON ({len(callers)} direct callers) - but "
                f"ALL of them are low-confidence name guesses, not resolved "
                f"references: this is an upper bound on who depends on you, "
                f"not a count of who does")
        elif callers_guessed:
            warnings.append(
                f"WIDELY DEPENDED ON ({len(callers)} direct callers, "
                f"{callers_guessed} of them low-confidence guesses): "
                f"signature/behavior changes ripple")
        else:
            warnings.append(f"WIDELY DEPENDED ON ({len(callers)} direct "
                            "callers): signature/behavior changes ripple")
    if radius["is_lower_bound"]:
        # NAME THE ACTUAL CAUSE. This said "dynamic event emission
        # reached" unconditionally, because that was the only way to be a
        # lower bound until 0.55.5 - so the moment unresolved references
        # could also set the flag, the sentence started explaining the
        # wrong thing to the reader most in need of the right one.
        why = []
        if b.get("radius_is_lower_bound"):
            why.append("dynamic event emission reached")
        if radius["unresolved_refs"]:
            why.append(f"{radius['unresolved_refs']} call reference"
                       f"{'s' if radius['unresolved_refs'] != 1 else ''} to "
                       f"this name could not be resolved to any entity")
        warnings.append("DOWNSTREAM REACH IS A LOWER BOUND: "
                        + " and ".join(why)
                        + " - real impact may exceed what the graph shows")
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

    _lag = _map_lag(repo, coord)
    if _lag:
        # before_edit is the briefing an agent reads before touching code.
        # A stale map here is not trivia, it is the difference between
        # "no callers" and "no callers as of seven commits ago".
        warnings = list(warnings) + [_lag["message"]]
    # Same rule as everywhere else; the deciding entry is never cut.
    _kn_shown, _kn_report = rank_bound_report(
        knowledge, "knowledge", rank=lambda r: r.get("superseded", False),
        cap=6)
    return {
        "map_lag": _lag,
        "knowledge_lag": _knowledge_lag(ix, meta),
        "entity": _entity_dict(e),
        "grounding": grounding,
        "inheritance": inheritance,
        "comments": comments_block,
        "design_docs": design_docs,
        "metrics": {"complexity": cx,
                    "fan_in": m.get("fan_in", 0),
                    "churn": m.get("churn", 0)},
        "direct_callers": callers_shown,
        **callers_report,
        "direct_callers_tests": callers_tests,
        "direct_callers_guessed": callers_guessed,
        "downstream": radius,
        "knowledge": _kn_shown,
        **_kn_report,
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
def _review_q(repo, a):
    """`memway --json review <repo> [REV]`, and it accepts `--since REV`.

    Somebody who learned the CLI form types the flag here, and the naive
    version answered "unknown revision '--since'" - technically true and
    useless. Taking both spellings costs two lines; sending a reader to
    diff the two surfaces by eye costs a session.
    """
    from .review import review
    args = list(a or [])
    since = "HEAD"
    for x in args:
        # NO BRANCH FOR `--since REV`. It had one, and no sabotage could
        # break it: the flag itself is skipped as a "--" token and the
        # value that follows is caught by the positional case below, so
        # the branch never decided anything. A branch a falsification
        # cannot reach is a branch doing no work.
        #
        # `--since=REV` DOES need its own case: it starts with "--", so
        # without this it is skipped and the revision silently stays at
        # the default.
        if x.startswith("--since="):
            since = x.split("=", 1)[1]
        elif not x.startswith("--"):
            since = x
    return review(repo, since)


def _search_q(repo, a):
    from .review import search
    if not a:
        return {"error": "usage: memway --json search <repo> <query> [channel]"}
    return search(repo, a[0], a[1] if len(a) > 1 else "")


QUERIES = {
    "show": lambda repo, a: show(repo, a[0]),
    # The one read that starts from a SUBJECT rather than a coordinate.
    # Third door, same function - the surfaces test requires all three.
    "search": lambda repo, a: _search_q(repo, a),
    # `memway review . --json` could never work: main() intercepts --json
    # before dispatch and reads the next token as a QUERY NAME, so the flag
    # on cmd_review was unreachable from the moment it was written. Same
    # shape as the --replay flag that existed and could not be discovered.
    # One door, the established one.
    "review": lambda repo, a: _review_q(repo, a),
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
    # NO LONGER THE ODD ONE OUT. Through 0.54.0 this re-indexed and
    # rewrote the edge cache "so the map reflects the tree it just
    # measured" - a deliberate, documented exception that made
    # `--json verify-change` a WRITE, five files' worth. 0.54.1 reverses
    # that decision: a read surface that mutates state could, after
    # 0.54.0, perform the sketch migration and announce it to a stdout a
    # --json caller never displays. It computes in memory now and is
    # enrolled in the read fence like everything else.
    "verify-change": lambda repo, a: verify_change(repo),
    # attention was MCP-only until 0.54.1 - not a query, not a command -
    # so anyone driving memway from the CLI could not ask the one question
    # that finds staled knowledge repo-wide.
    "attention": lambda repo, a: attention(repo),
}


def _dig(repo, ref):
    from .dig import dig as _d
    return _d(repo, ref)


def verify_change(repo_root, run=False):
    """MCP/CLI entry: post-change impact, test selection, and the knowledge
    this change invalidated (see verify.py).

    A PURE READ, as of 0.54.1. It used to write five files - the index, the
    edges, the parse cache and both pickles - because it re-indexed the
    working tree and then saved. It was never enrolled in the read fence,
    so nothing caught it; a "read" that reindexes could, after 0.54.0, even
    perform the sketch migration and announce it to a stdout that a --json
    caller has no reason to display.

    STALED KNOWLEDGE IS THE POINT. This is the step the workflow rules send
    you to after an edit, and it used to report blast radius and tests while
    saying nothing about the notes the edit had just invalidated - so the
    loop never closed and staleness was discovered later, by whoever
    happened to open a map. Five notes on memway's own flagship went stale
    that way and sat coral on the public site.

    The report is computed against the WORKING TREE, not the stored index:
    index(persist=False) recomputes hashes in memory for changed files. At
    the moment you would ask - edited, not re-indexed, not committed - the
    stored index still holds the OLD hashes and would report everything
    fresh. That is the trap this exists to avoid.
    """
    from pathlib import Path
    from .indexer import Indexer
    from .edges import EdgeBuilder
    from .metadata import MetaStore, accepted_for
    from .verify import verify_change as _vc
    repo_root = Path(repo_root)
    coord = repo_root / ".coord"
    ix = Indexer(repo_root, coord)
    # BOTH loaders, or the fence still fails. load_existing warms
    # coordinates.pkl and EdgeBuilder.load warms edges.pkl; suppressing
    # only the first left edges.pkl behind, which is precisely how the
    # earlier leaks in this file survived - there are exactly two
    # cache-warming loaders on this path and missing either one is a write.
    # read_only() is belt AND braces: the loader suppression below already
    # makes this inert, proven by the fence. But it protects only what it
    # names - if anyone adds a harvest_docs call to this path, the
    # docbindings baseline write comes back and the suppression says
    # nothing about it. Durability insurance, inert today.
    with read_only():
        ix.load_existing(write_cache=False)
        eb = EdgeBuilder(ix)
        edges = EdgeBuilder.load(coord, write_cache=False) or []
        result = _vc(ix, edges or eb.build(), repo_root, run=run,
                     persist=False)

    # Which knowledge did this change invalidate? Only entries that are the
    # NEWEST in their channel: an older entry somebody already superseded
    # is history, and re-reporting it would drown the one that needs an
    # answer. Same rule the ring uses, one implementation.
    meta = MetaStore(coord)
    staled = []
    for cid in dict.fromkeys(result.get("changed_ids", [])):
        ent = ix.entities.get(cid)
        if not ent:
            continue
        rows = for_display(meta.read_all(cid, accepted_for(ent)))
        for row in unsuperseded_stale(rows):
            staled.append({
                "coordinate": cid,
                "qualname": ent.qualname,
                # CHANNEL IS REQUIRED. Superseding only heals when the
                # fresh entry lands in the SAME channel - a confirm does
                # not answer a stale note. A report without it sends the
                # reader to write an entry that changes nothing.
                "channel": row.get("channel", ""),
                "text": row.get("text", ""),
            })
    result["staled_knowledge"] = staled

    # KNOWLEDGE ON THE CALLERS, one layer out. staled_knowledge asks what
    # the change invalidated ON the changed entity - the stamp says so.
    # But a note on a CALLER that names the thing you just changed is the
    # next most likely to be quietly wrong, and no stamp will ever catch
    # it: the caller's own body did not move, so its entries stay fresh
    # while the fact they describe has changed underneath.
    #
    # A HEURISTIC, LABELLED AS ONE. Mentioning a name is not evidence a
    # note is wrong; it is evidence of where to look. So these are
    # reported separately from staled_knowledge, never counted with it,
    # and `--gate` does not block on them. A guess promoted to a verdict
    # is the failure this project keeps finding in its own output.
    at_risk = []
    changed_names = {}
    for cid in dict.fromkeys(result.get("changed_ids", [])):
        ent = ix.entities.get(cid)
        if ent:
            changed_names[_short(ent.qualname)] = ent.qualname
    if changed_names:
        changed_set = set(result.get("changed_ids", []))
        callers: dict = {}
        for e in edges:
            if e.get("kind") != "calls":
                continue
            if e["dst"] in changed_set and e["src"] not in changed_set:
                callers.setdefault(e["src"], set()).add(e["dst"])
        for src, dsts in callers.items():
            src_ent = ix.entities.get(src)
            if not src_ent:
                continue
            rows = for_display(meta.read_all(src, accepted_for(src_ent)))
            for row in rows:
                if row.get("superseded"):
                    continue
                text_l = (row.get("text") or "").lower()
                named = sorted({changed_names[n] for d in dsts
                                for n in [_short(ix.entities[d].qualname)]
                                if n and n.lower() in text_l})
                if named:
                    at_risk.append({
                        "coordinate": src,
                        "qualname": src_ent.qualname,
                        "channel": row.get("channel", ""),
                        "mentions": named,
                        "text": row.get("text", ""),
                    })
    shown_risk, risk_report = rank_bound_report(
        at_risk, "knowledge_at_risk",
        rank=lambda k: (-len(k["mentions"]), k["qualname"]))
    result["knowledge_at_risk"] = shown_risk
    result.update(risk_report)
    result["knowledge_at_risk_note"] = (
        "notes on CALLERS that name something this change touched. Their "
        "own stamps are still fresh - the caller's body did not move - so "
        "nothing else will flag them. A mention is where to look, not "
        "proof of error, and --gate does not block on these."
    )

    # Which COMMENTS did this change rot? The same question one layer out:
    # staled_knowledge asks what the change invalidated in the map, this
    # asks what it invalidated in the source. Both are caught at the commit
    # that causes them, which is the only moment the author still has the
    # reasoning in their head.
    #
    # SCOPED TO changed_ids, exactly like staled_knowledge, and this is the
    # whole design. This repo carries a 49-item rot backlog; a commit-time
    # report that listed all of it would be scrolled past within a week,
    # and the ring rule already taught that lesson the expensive way -
    # attention read 43 when 3 were actionable and stopped being worked.
    # The backlog lives in `memway attention`, which is a queue you visit.
    # This is an alarm, and an alarm that fires on other people's work is
    # not an alarm.
    #
    # Free by construction: verify.verify_change calls index(persist=False),
    # so ix.entities already holds WORKING-TREE entities whose comment_rot
    # was computed against the stored map. This reports a verdict that was
    # already sitting there; it computes nothing.
    # REDUNDANT SINCE 0.56.1, AND KEPT DELIBERATELY. Modules no longer
    # carry comment_rot at all - the flag ends at the computation in
    # indexer._assign, because a module docstring's claims range over the
    # file and beyond it and no hash can bound them. So this branch can no
    # longer fire.
    #
    # It stays as a second guard rather than being deleted, and it says so
    # rather than reading like a live rule: 0.55.4 added it because module
    # rot could not be answered (a confirm staled on the next commit to
    # that file), and if module rot ever returns under an honestly-named
    # prompt, the commit-time alarm must still not carry it.
    rotted = []
    for cid in dict.fromkeys(result.get("changed_ids", [])):
        ent = ix.entities.get(cid)
        if not ent or not getattr(ent, "comment_rot", False):
            continue
        if ent.kind == "module":
            continue
        if rot_is_answered(meta.read_all(cid, accepted_for(ent))):
            continue
        rotted.append({
            "coordinate": cid,
            "qualname": ent.qualname,
            "path": ent.path,
            "line": ent.lineno,
            "comments": [c.get("text", "") for c in
                         (getattr(ent, "comments", None) or [])][:3],
        })
    result["rotted_comments"] = rotted
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

    The entry is stamped at write time by stamp_for() - the LOGIC hash
    where the language has one, falling back to the body hash - so the
    note survives comment and docstring edits and is flagged stale only
    when behaviour moves. Author attribution keeps agent notes
    distinguishable from human ones.

    Said "body-hash-stamped" here until 2026-08-16, which stopped being
    true when stamping was unified in aa77673. The inline comment at the
    stamp site was right the whole time; this docstring and the returned
    note were not, and the returned note is what every caller reads.
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
             body_hash=stamp_for(e, repo_root))
    return {
        "attached": {"coord": e.coord_id, "qualname": e.qualname,
                     "channel": channel, "author": author},
        "note": "entry is stamped with the entity's logic hash; it will be "
                "flagged stale when behaviour changes, not when comments do",
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
            # ASKED, not restated - the same rule verify_change uses to
            # report rot at the commit that caused it.
            md = meta.read_all(e.coord_id, current_hash=accepted_for(e))
            if not rot_is_answered(md):
                rot.append(e.qualname)
    # NO SILENT CAPS. This truncated to `limit` and reported no total,
    # while markers - built three lines down, into the same payload -
    # shipped marker_total all along. So a reader saw 20 and could not
    # tell whether that was the census or the first page of it: this
    # repo's real backlog was 49, and two independent readers on the same
    # day reported 20 and 10 as if they were totals. The rest of the
    # codebase already keeps this rule (downstream's is_lower_bound, the
    # shallow-clone label on dig, get_parsers naming every skipped
    # language); one surface in this very function kept it and its
    # neighbour did not.
    # THROUGH THE ONE FUNCTION. This was the third hand-written copy of
    # rank-bound-report in this module; markers below was the fourth,
    # and summary held two more that reported nothing at all.
    rot, rot_report = rank_bound_report(rot, "comment_rot", cap=limit)

    # ONE MARKER, ONE ENTITY - the innermost that contains it.
    #
    # Comments are attributed by line containment, so a FIXME inside a
    # method belongs to the method AND its class AND its module, and the
    # queue listed it three times. Measured on a three-comment fixture:
    # six markers. That is the attention queue inflating its own count,
    # which is how a queue stops being worked - the same disease as the
    # 43-vs-3 incident recorded on this function.
    #
    # (file, line) identifies the comment; the entity with the SMALLEST
    # span containing that line is the one that owns it. Exact, because
    # comments carry their line - not a guess from matching text.
    _claims: dict = {}
    for e in ix.entities.values():
        for c in getattr(e, "comments", []) or []:
            mk = _re.match(r"(TODO|FIXME|HACK|XXX)\b[:\s]*(.*)",
                           c["text"], _re.I)
            if not mk:
                continue
            # THE LINE IS ENTITY-RELATIVE, not absolute in the file -
            # measured, because keying on it directly deduped nothing:
            # the same FIXME reads line 5 on the module, 4 on the class
            # and 2 on the method. lineno + line - 1 gives 5 for all
            # three, which is what makes them one marker.
            try:
                rel = int(c.get("line") or 0)
            except (TypeError, ValueError):
                rel = 0
            line = (e.lineno or 1) + rel - 1 if rel else 0
            key = (e.path, line, mk.group(1).upper(), mk.group(2)[:100])
            span = (getattr(e, "end_lineno", 0) or 0) - (e.lineno or 0)
            prev = _claims.get(key)
            if prev is None or span < prev[0]:
                _claims[key] = (span, {"tag": mk.group(1).upper(),
                                       "entity": e.qualname,
                                       "text": mk.group(2)[:100]})
    markers = [v for _, v in _claims.values()]
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

    # THE RING RULE, asked - not restated. This counted every entry
    # carrying en["stale"] by hand, so superseded history counted as a
    # warning: the flagship read "43 stale knowledge entries" when the
    # decisive queue was 3, and 43 was exactly the number of entries that
    # had a newer entry behind them. Ambient _knowledge_lag, reading the
    # same bytes through unsuperseded_stale, said 3 the whole time - one
    # surface contradicting another about one number, which is how a
    # queue stops being worked. Same rule, same answer, one caller.
    stale_notes = 0
    for e in ix.entities.values():
        rows = for_display(meta.read_all(e.coord_id,
                                         current_hash=accepted_for(e)))
        stale_notes += len(unsuperseded_stale(rows))

    markers_shown, markers_report = rank_bound_report(
        markers, "markers", cap=limit)

    return {
        "comment_rot": rot,
        **rot_report,
        "markers": markers_shown,
        **markers_report,
        # marker_total is kept as it was: the MCP has shipped that key and
        # renaming a payload field to tidy an internal refactor would break
        # a caller for nobody's benefit.
        "marker_total": markers_report["markers_total"],
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
