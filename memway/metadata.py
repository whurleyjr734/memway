"""
Per-coordinate metadata store.

Each coordinate ID owns a folder under .coord/meta/<coord_id>/ containing
typed metadata channels. Knowledge attaches to the stable ID, not to a
file path, so it survives renames and refactors (via lineage).

Channels:
  history  - what changed and when (append-only)
  design   - why it is built this way
  notes    - gotchas, invariants, warnings
  docs     - documentation
  traces   - recorded data-flow traversals (structural stack traces)
  confirm  - attestation that current comments/docs remain accurate after logic change
"""

import json
import time
from pathlib import Path

CHANNELS = ("history", "design", "notes", "docs", "traces", "confirm")


class MetaStore:
    def __init__(self, coord_dir: str):
        self.root = Path(coord_dir) / "meta"

    def _channel_file(self, coord_id: str, channel: str) -> Path:
        if channel not in CHANNELS:
            raise ValueError(f"unknown channel {channel!r}; use one of {CHANNELS}")
        d = self.root / coord_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{channel}.jsonl"

    def add(self, coord_id: str, channel: str, text: str,
            author: str = "human", body_hash: str = "", **extra):
        """Append an entry to a coordinate's metadata channel.
        D10: entries are stamped with the entity's body_hash at write
        time; when the code later changes, the stamp mismatch marks the
        entry stale (trust decays, entries never silently vanish)."""
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "author": author, "text": text}
        if body_hash:
            entry["body_hash"] = body_hash
        entry.update(extra)
        f = self._channel_file(coord_id, channel)
        with f.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def read(self, coord_id: str, channel: str,
             current_hash="") -> list[dict]:
        f = self.root / coord_id / f"{channel}.jsonl"
        if not f.exists():
            return []
        out = [json.loads(line) for line in f.read_text().splitlines()
               if line]
        if current_hash:
            # current_hash may be a single hash or an iterable of accepted
            # hashes (e.g. {logic_hash, body_hash}); a note is fresh if its
            # stamp matches ANY accepted hash. Lets logic-stamped notes
            # survive cosmetic edits, and old body-stamped notes stay valid
            # until the text actually changes.
            accepted = ({current_hash} if isinstance(current_hash, str)
                        else set(current_hash))
            accepted.discard("")
            for e in out:
                if e.get("body_hash") and e["body_hash"] not in accepted:
                    e["stale"] = True
        return out

    def read_all(self, coord_id: str, current_hash="") -> dict:
        return {ch: entries for ch in CHANNELS
                if (entries := self.read(coord_id, ch, current_hash))}

    def migrate(self, old_id: str, new_id: str, note: str = "",
                new_body_hash: str = ""):
        """Move metadata from one coordinate to another (used by lineage
        when an entity is renamed/split and knowledge must follow)."""
        old_dir = self.root / old_id
        if not old_dir.exists():
            return
        new_dir = self.root / new_id
        new_dir.mkdir(parents=True, exist_ok=True)
        for f in old_dir.glob("*.jsonl"):
            target = new_dir / f.name
            lines = f.read_text().splitlines()
            with target.open("a") as out:
                for line in lines:
                    if not line:
                        continue
                    # detected renames preserve semantics (shape match),
                    # so migrated entries are re-stamped to the new hash
                    # rather than falsely flagged stale
                    if new_body_hash:
                        e = json.loads(line)
                        if e.get("body_hash"):
                            e["body_hash"] = new_body_hash
                        line = json.dumps(e)
                    out.write(line + "\n")
        self.add(new_id, "history",
                 f"metadata inherited from {old_id}. {note}".strip(),
                 author="lineage")


class TraceRecorder:
    """Records data-flow traversals as sequences of coordinates.

    This is the 'structural stack trace': instead of log lines (the
    author's guesses about what matters), we record where data actually
    went, coordinate by coordinate, with optional payload notes.
    """

    def __init__(self, meta: MetaStore, trace_id: str):
        self.meta = meta
        self.trace_id = trace_id
        self.hops: list[dict] = []

    def hop(self, coord_id: str, note: str = "", payload_summary: str = ""):
        self.hops.append({
            "seq": len(self.hops),
            "coord": coord_id,
            "note": note,
            "payload": payload_summary,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        return self

    def commit(self):
        """Write the full trace to every coordinate it touched."""
        path = [h["coord"] for h in self.hops]
        for h in self.hops:
            self.meta.add(h["coord"], "traces",
                          h["note"] or "(hop)",
                          author="trace",
                          trace_id=self.trace_id,
                          seq=h["seq"],
                          full_path=path,
                          payload=h["payload"])
        return path
