"""
Versioning + lineage: identity through time.

Versioning alone gives you snapshots (a photo album). Lineage is the
thread connecting them (the biography): explicit records that coordinate
X in v(N) descends from coordinate Y in v(N-1).

Lineage records are best written at edit time by whoever makes the change
(a human or an editing agent), because at that moment identity is known
with certainty. This module also does best-effort automatic detection:
a removed entity and an added entity with the same body hash (or same
name in a new location) are matched as a rename/move.
"""

import json
import shutil
import time
from pathlib import Path

from .indexer import Indexer, Entity
from .metadata import MetaStore


class VersionStore:
    def __init__(self, coord_dir: str):
        self.coord_dir = Path(coord_dir)
        self.versions_dir = self.coord_dir / "versions"
        self.lineage_log = self.coord_dir / "lineage" / "lineage.jsonl"

    # ------------------------------------------------------------ versions

    def current_version(self) -> int:
        if not self.versions_dir.exists():
            return 0
        nums = [int(p.name[1:]) for p in self.versions_dir.glob("v*")
                if p.name[1:].isdigit()]
        return max(nums, default=0)

    def snapshot(self) -> int:
        """Copy the current index into versions/v<N+1>/."""
        v = self.current_version() + 1
        dst = self.versions_dir / f"v{v}"
        dst.mkdir(parents=True, exist_ok=True)
        src = self.coord_dir / "index"
        for f in src.glob("*.json"):
            shutil.copy(f, dst / f.name)
        (dst / "meta.json").write_text(json.dumps({
            "version": v,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }))
        return v

    # ------------------------------------------------------------- lineage

    def record(self, kind: str, old_ids: list[str], new_ids: list[str],
               note: str = "", author: str = "auto"):
        """kind: renamed | moved | split | merged | deleted | created"""
        self.lineage_log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "version": self.current_version(),
            "kind": kind,
            "old": old_ids,
            "new": new_ids,
            "note": note,
            "author": author,
        }
        with self.lineage_log.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def read(self) -> list[dict]:
        if not self.lineage_log.exists():
            return []
        return [json.loads(l) for l in
                self.lineage_log.read_text().splitlines() if l]

    def ancestry(self, coord_id: str) -> list[dict]:
        """Walk lineage backwards from a coordinate."""
        chain, frontier = [], {coord_id}
        for entry in reversed(self.read()):
            if frontier & set(entry["new"]):
                chain.append(entry)
                frontier |= set(entry["old"])
        return chain


def detect_lineage(report: dict, indexer: Indexer,
                   store: VersionStore, meta: MetaStore) -> list[dict]:
    """Best-effort automatic lineage from an index report.

    Matches removed entities to added entities:
      1. identical body hash            -> renamed/moved (high confidence)
      2. same short name, same kind     -> probable move (lower confidence)
    Matched entities get their metadata migrated to the new coordinate.
    """
    detected = []
    removed: dict[str, Entity] = report.get("removed_entities", {})
    added_ids = list(report.get("added", []))
    unmatched_removed = dict(removed)

    # pass 1: body-hash match
    for old_id, old_ent in list(unmatched_removed.items()):
        for new_id in added_ids:
            new_ent = indexer.entities.get(new_id)
            if new_ent and new_ent.body_hash == old_ent.body_hash \
                    and new_ent.kind == old_ent.kind:
                entry = store.record(
                    "renamed", [old_id], [new_id],
                    note=f"{old_ent.qualname} -> {new_ent.qualname} "
                         f"(identical body)", author="auto")
                meta.migrate(old_id, new_id,
                             note=f"followed rename from {old_ent.qualname}")
                detected.append(entry)
                del unmatched_removed[old_id]
                added_ids.remove(new_id)
                break

    # pass 1.2: logic-hash match - identical behavior, cosmetics differ.
    # Catches rename-plus-docstring/comment-tweak commits that the body
    # pass misses, at the same certainty tier (logic identity IS identity).
    for old_id, old_ent in list(unmatched_removed.items()):
        olh = getattr(old_ent, "logic_hash", "")
        if not olh:
            continue
        for new_id in added_ids:
            new_ent = indexer.entities.get(new_id)
            if new_ent and getattr(new_ent, "logic_hash", "") == olh \
                    and new_ent.kind == old_ent.kind:
                entry = store.record(
                    "renamed", [old_id], [new_id],
                    note=f"{old_ent.qualname} -> {new_ent.qualname} "
                         f"(identical logic)", author="auto")
                meta.migrate(old_id, new_id,
                             note=f"followed rename from {old_ent.qualname}",
                             new_body_hash=getattr(new_ent, "logic_hash", ""))
                detected.append(entry)
                del unmatched_removed[old_id]
                added_ids.remove(new_id)
                break

    # ---- scored matching over the remainder (replaces passes 1.5 / 2) ----
    # Signals available WITHOUT old source text: short-name similarity,
    # AST shape equality, minhash Jaccard, signature similarity, loc ratio.
    # Confident matches -> author="auto" + metadata migration.
    # Uncertain matches -> author="pending-review", NO migration until a
    # human or edit-time agent confirms (a wrong migration is the poison).
    from difflib import SequenceMatcher
    from .indexer import sketch_jaccard, sketch_containment

    def _r(a, b):
        return SequenceMatcher(None, a or "", b or "").ratio()

    def _score(o, n):
        name_s = _r(o.qualname.rsplit(".", 1)[-1], n.qualname.rsplit(".", 1)[-1])
        jac = sketch_jaccard(o.sketch, n.sketch)
        shape = 1.0 if (o.shape_hash and o.shape_hash == n.shape_hash) else 0.0
        sig = _r(o.signature, n.signature)
        loc = min(o.loc, n.loc) / max(o.loc, n.loc, 1)
        return (0.22 * name_s + 0.30 * jac + 0.22 * shape
                + 0.13 * sig + 0.13 * loc), name_s, jac, shape

    olds = list(unmatched_removed.items())
    news = [(nid, indexer.entities[nid]) for nid in added_ids
            if indexer.entities.get(nid)
            and indexer.entities[nid].kind in ("function", "method", "class")]
    news = [(nid, ne) for nid, ne in news]
    S = {}
    for oid, oe in olds:
        for nid, ne in news:
            if ne.kind != oe.kind:
                continue
            S[(oid, nid)] = _score(oe, ne)

    used_o, used_n = set(), set()
    # one-to-many evidence beats one-to-one: a removed entity with 2+
    # containment children is a split suspect - pair matching must not
    # consume it before the split detector sees it.
    split_suspects = set()
    for oid, oe in olds:
        if not oe.sketch:
            continue
        kids = sum(1 for nid, ne in news if ne.sketch and
                   sketch_containment(ne.sketch, oe.sketch,
                                      ne.n_shingles, oe.n_shingles) >= 0.5)
        if kids >= 2:
            split_suspects.add(oid)
    ranked = sorted(S.items(), key=lambda kv: -kv[1][0])
    deferred = []                      # weak candidates: decided AFTER split/merge
    for (oid, nid), (sc, name_s, jac, shape) in ranked:
        if oid in used_o or nid in used_n or sc < 0.33:
            continue
        row2 = max((v[0] for k, v in S.items()
                    if k[0] == oid and k[1] != nid and k[1] not in used_n),
                   default=0.0)
        col2 = max((v[0] for k, v in S.items()
                    if k[1] == nid and k[0] != oid and k[0] not in used_o),
                   default=0.0)
        margin = sc - max(row2, col2)
        corroborated = shape or name_s >= 0.60 or jac >= 0.45
        oe = unmatched_removed[oid]; ne = indexer.entities[nid]
        sig_s = _r(oe.signature, ne.signature)
        note = f"{oe.qualname} -> {ne.qualname} (score={sc:.2f})"
        # exact short name + matching signature: probable move even with a
        # rewritten body. Migration is safe here because migrated metadata
        # is body-hash-stamped and the staleness net flags it downstream.
        name_move = name_s == 1.0 and sig_s >= 0.85
        if oid in split_suspects and not name_move:
            deferred.append((oid, nid, note))
            continue
        if (sc >= 0.55 and corroborated and margin >= 0.05) or name_move:
            kind_ = "renamed" if oe.path == ne.path else "moved"
            if name_move and sc < 0.55:
                note += " (same name, body changed - verify)"
            entry = store.record(kind_, [oid], [nid], note=note, author="auto")
            # identical AST shape => semantics preserved => re-stamp the
            # migrated notes with the new body hash so they are not
            # flagged stale (matches original pass-1.5 behavior).
            if shape:
                meta.migrate(oid, nid,
                             note=f"followed refactor from {oe.qualname}",
                             new_body_hash=ne.body_hash)
            else:
                meta.migrate(oid, nid,
                             note=f"followed refactor from {oe.qualname}")
            detected.append(entry)
            used_o.add(oid); used_n.add(nid)
        elif margin >= 0.03:
            deferred.append((oid, nid, note))   # weak: let split/merge claim first

    # ---- split / merge recovery via containment estimates ----
    for oid, oe in olds:
        if oid in used_o or not oe.sketch:
            continue
        children = []
        for nid, ne in news:
            if nid in used_n or not ne.sketch:
                continue
            if sketch_containment(ne.sketch, oe.sketch,
                                  ne.n_shingles, oe.n_shingles) >= 0.5:
                children.append(nid)
        if children:
            detected.append(store.record(
                "split", [oid], children,
                note=f"{oe.qualname} -> "
                     f"{[indexer.entities[c].qualname for c in children]}"
                     f" - needs confirmation",
                author="pending-review"))
            used_o.add(oid); used_n.update(children)
    for nid, ne in news:
        if nid in used_n or not ne.sketch:
            continue
        parents = [oid for oid, oe in olds
                   if oid not in used_o and oe.sketch
                   and sketch_containment(oe.sketch, ne.sketch,
                                          oe.n_shingles, ne.n_shingles) >= 0.5]
        if parents:
            detected.append(store.record(
                "merged", parents, [nid],
                note=f"-> {ne.qualname} - needs confirmation",
                author="pending-review"))
            used_o.update(parents); used_n.add(nid)

    # pending tier: only pairs split/merge did not explain better
    for oid, nid, note in deferred:
        if oid in used_o or nid in used_n:
            continue
        oe = unmatched_removed[oid]; ne = indexer.entities[nid]
        detected.append(store.record(
            "renamed" if oe.path == ne.path else "moved",
            [oid], [nid], note=note + " - verify, needs confirmation",
            author="pending-review"))
        used_o.add(oid); used_n.add(nid)

    for oid in list(unmatched_removed):
        if oid in used_o:
            del unmatched_removed[oid]

    # whatever is left is a true deletion
    for old_id, old_ent in unmatched_removed.items():
        detected.append(store.record(
            "deleted", [old_id], [],
            note=old_ent.qualname, author="auto"))

    return detected
