"""
Flight recorder: local-only usage log of MCP tool calls.

One JSONL line per call in .coord/log/usage.jsonl:
    {ts, session, tool, ref, ok}

Purpose: tool-combination patterns should be DISCOVERED from evidence,
not designed from intuition - the sequences agents actually run are
the data. Collection is the irreversible part (unlogged history is
gone forever), so this records from day one and analyzes nothing.

Trust contract:
  - the log never leaves .coord/ (same trust domain as the index);
    there is no telemetry, no phone-home, and never will be
  - plain greppable JSONL, like everything else in the map
  - only the tool name and the entity REFERENCE are recorded, never
    payload text (a note's content does not belong in a usage log)
  - a recorder failure must NEVER fail the tool call it observes
"""

import json
import time
import uuid
from pathlib import Path

# One id per server process: calls within a session are groupable,
# which is what makes SEQUENCES (the interesting patterns) visible.
SESSION = uuid.uuid4().hex[:12]

_REF_KEYS = ("ref", "entity", "coordinate", "path", "channel")


def record(repo: str, tool: str, arguments: dict, ok: bool = True):
    """Append one usage line. Silent no-op on any failure, and on
    repos that were never initialized (the recorder observes maps,
    it does not create them)."""
    try:
        coord = Path(repo) / ".coord"
        if not coord.exists():
            return
        d = coord / "log"
        d.mkdir(parents=True, exist_ok=True)
        ref = ""
        for k in _REF_KEYS:
            v = (arguments or {}).get(k)
            if isinstance(v, str) and v:
                ref = v
                break
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "session": SESSION, "tool": tool, "ref": ref, "ok": ok}
        with (d / "usage.jsonl").open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # the recorder must never crash the plane
