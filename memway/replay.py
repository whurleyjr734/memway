"""Carry a bundle's knowledge onto YOUR checkout.

THE PROBLEM. A published bundle records `upstream_sha` - the exact commit
it was indexed from - and `pull` installs that bundle's index wholesale.
If your checkout is at a different commit, you now hold a map of code you
do not have, and the only thing the tool says about it is
`drifted: true`. The structure is describing one version while your tree
is another.

THE ASYMMETRY THAT SOLVES IT. A bundle is two things of very different
value. Its index - 871 KB for flask - regenerates locally in seconds and
is worth nothing shipped. Its authored knowledge is the part nobody can
reconstruct: somebody's reason for something, written once. So the honest
model is not "adopt the bundle's map" but "index what you actually have,
and replay the knowledge onto it".

MATCHING. A coordinate is sha256(qualname), so anything not renamed
between the two versions matches EXACTLY - the common case across a minor
release. What moved falls to lineage.score_pair, the same signal mix
(name, shape, signature, size, minhash sketch) used to follow renames
inside one repo. Bundles carry every one of those signals; they were
being shipped and ignored.

WHY THIS IS SAFE, AND THE PART TO NOT BREAK. Replayed entries keep their
ORIGINAL body_hash. That is the whole design: a note authored against
flask 3.0 and replayed onto 3.1 carries a stamp that no longer matches,
so it reads STALE and says "this was written about older code, verify
it". Re-stamping would silently assert it still holds - which is exactly
the lie this project exists to prevent. Preserving the stamp turns
version skew into honest staleness rather than confident wrongness, and
nothing new had to be invented for it.

Nothing here writes an index. It reads a staged bundle and appends to the
local metadata store, and it reports every coordinate it could not place
rather than dropping it quietly.
"""
from __future__ import annotations

import json
from pathlib import Path

from .metadata import MetaStore, CHANNELS

# lineage's own confident-match threshold. Kept identical on purpose: a
# rename is a rename whether it happened inside one repo between two
# indexes or between two published versions of the same project, and two
# numbers for one judgement is how they drift apart.
CONFIDENT = 0.55


class _BundleEntity:
    """The attributes score_pair reads, off a bundle's JSON record.

    Deliberately not memway.indexer.Entity: constructing one would demand
    every field the dataclass has and would break the moment Entity gains
    another. This carries exactly the signals the matcher asks for, which
    is also the honest statement of what a bundle needs to ship.
    """

    __slots__ = ("coord_id", "qualname", "kind", "sketch", "shape_hash",
                 "signature", "loc")

    def __init__(self, rec: dict):
        from .indexer import decode_sketch
        self.coord_id = rec.get("coord_id", "")
        self.qualname = rec.get("qualname", "")
        self.kind = rec.get("kind", "")
        self.sketch = decode_sketch(rec.get("sketch"))
        self.shape_hash = rec.get("shape_hash", "")
        self.signature = rec.get("signature", "")
        self.loc = rec.get("loc", 0) or 0


def _bundle_entities(bundle_coord: Path) -> dict:
    p = bundle_coord / "index" / "coordinates.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    recs = data.values() if isinstance(data, dict) else data
    out = {}
    for r in recs:
        e = _BundleEntity(r)
        if e.coord_id:
            out[e.coord_id] = e
    return out


def _bundle_knowledge(bundle_coord: Path) -> dict:
    """coord_id -> {channel: [entry dicts]}, straight off the JSONL."""
    meta_root = bundle_coord / "meta"
    if not meta_root.is_dir():
        return {}
    out: dict = {}
    for cdir in sorted(meta_root.iterdir()):
        if not cdir.is_dir():
            continue
        for f in sorted(cdir.glob("*.jsonl")):
            if f.stem not in CHANNELS:
                continue
            entries = []
            for line in f.read_text().splitlines():
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue          # a damaged line is not the whole file
            if entries:
                out.setdefault(cdir.name, {})[f.stem] = entries
    return out


def _best_local_match(be, local_entities, by_kind):
    """The local entity this bundle entity most likely became, or None."""
    from .lineage import score_pair
    best, best_score = None, 0.0
    for le in by_kind.get(be.kind, ()):
        try:
            score, _, _, _ = score_pair(be, le, use_sketch=bool(be.sketch))
        except Exception:
            continue                       # a signal we cannot read is not a match
        if score > best_score:
            best, best_score = le, score
    return (best, best_score) if best_score >= CONFIDENT else (None, best_score)


def _identity(en: dict):
    """What makes two entries THE SAME ENTRY, for deduplication.

    Text, for a claim - two identical sentences in a channel are one
    observation, and replaying a bundle twice must not double them.

    A STAMP HAS NO TEXT, so text cannot identify it. Every re-stamp
    carries `text: ""` (metadata.MetaStore.reaffirm), which made them all
    identical to each other under the old rule, and the failure was not
    cosmetic: pull an updated map whose upstream had re-affirmed a note at
    a NEW hash, and that stamp was discarded because an older one - at a
    different hash - had already put "" in the set. The note then read
    STALE on the puller's map when upstream had just re-checked it, which
    is the exact inversion of what this module promises. Version skew is
    supposed to read as honest staleness; here it read as staleness that
    was not true.

    So a stamp is identified by the hash it vouches at. Claims are
    untouched by this, deliberately: keying them on (text, hash) too would
    replay the same sentence again every time the code moved.
    """
    if en.get("reaffirms"):
        return ("\x00reaffirms", en.get("body_hash", ""))
    return en.get("text")


def replay(bundle_coord: Path, local_coord: Path, local_indexer) -> dict:
    """Append the bundle's knowledge to the local map. Returns a report.

    Never deletes and never re-stamps. An entry that already exists on the
    target coordinate is not duplicated, so replaying twice is a no-op
    rather than a doubling. What counts as "already there" is _identity -
    text for a claim, the vouched-for hash for a stamp.
    """
    bundle_coord, local_coord = Path(bundle_coord), Path(local_coord)
    b_ents = _bundle_entities(bundle_coord)
    b_know = _bundle_knowledge(bundle_coord)
    store = MetaStore(local_coord)

    local_entities = dict(local_indexer.entities)
    by_kind: dict = {}
    for le in local_entities.values():
        by_kind.setdefault(le.kind, []).append(le)

    report = {"exact": 0, "matched": 0, "orphaned": 0,
              "entries_replayed": 0, "entries_already_present": 0,
              "matches": [], "orphans": []}

    for cid, channels in sorted(b_know.items()):
        target, how, score = None, "", 0.0
        if cid in local_entities:
            target, how = local_entities[cid], "exact"
        else:
            be = b_ents.get(cid)
            if be is not None:
                target, score = _best_local_match(be, local_entities, by_kind)
                how = "lineage" if target is not None else ""

        if target is None:
            report["orphaned"] += 1
            be = b_ents.get(cid)
            report["orphans"].append({
                "coordinate": cid,
                "qualname": getattr(be, "qualname", "") or "(unknown)",
                "entries": sum(len(v) for v in channels.values()),
                "best_score": round(score, 2),
            })
            continue

        report["exact" if how == "exact" else "matched"] += 1
        if how == "lineage":
            report["matches"].append({
                "from": getattr(b_ents.get(cid), "qualname", cid),
                "to": target.qualname, "score": round(score, 2)})

        for channel, entries in channels.items():
            existing = {_identity(e) for e in store.read(target.coord_id,
                                                        channel)}
            for en in entries:
                if _identity(en) in existing:
                    report["entries_already_present"] += 1
                    continue
                extra = {k: v for k, v in en.items()
                         if k not in ("ts", "author", "text", "body_hash")}
                # THE STAMP IS CARRIED, NOT REWRITTEN. See the module
                # docstring: this is what makes version skew read as
                # honest staleness instead of a false claim of currency.
                store.add(target.coord_id, channel, en.get("text", ""),
                          author=en.get("author", "inherited"),
                          body_hash=en.get("body_hash", ""), **extra)
                report["entries_replayed"] += 1
    return report
