#!/usr/bin/env python3
"""Stop hook: refuse to end a working session that recorded nothing.

Rules in a context file are advisory.
They are followed reliably when the agent is already in a tool-using loop
(edit -> verify -> capture), and skipped when the session ends by
answering the human: an investigation or a refusal has no end-of-work
moment, so the capture step is never reached. Three rewrites of the
trigger wording did not change that, because the wording is not the
binding constraint - being asked at the right moment is.

This is that moment. It fires on Stop, checks whether memway_meta was
called anywhere in the session, and if not, blocks once with a reason.

Deliberately conservative - it stays silent when:
  * stop_hook_active is set (we already blocked once; never loop)
  * the transcript is missing or unreadable
  * the session used no tools at all (a conversation, not work)
Silence is the default so that being blocked actually means something.
"""

import json
import os
import re
import sys

# Match the tool_use NAME, not the string anywhere. A bare substring
# search passes on any session that merely READS the marker - CLAUDE.md
# names memway_meta five times, mcp.py and test_mcp.py once each, and the
# permissions allow-list names it too. Measured on one real transcript:
# 207 occurrences of the string, 8 actual calls.
CALL_RE = re.compile(r'"name"\s*:\s*"(?:mcp__[A-Za-z0-9_]*__)?memway_meta"')
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROT_REASON = (
    "These entities were edited this session and now carry comment_rot - "
    "their comments describe behavior the code no longer has:\n\n{items}\n\n"
    "before_edit cannot warn you about this: it runs before the edit, when "
    "the flag does not yet exist, and verify_change does not report rot at "
    "all. Either update each comment to describe the new behavior, or write "
    "a confirm entry saying why it still holds. Do not touch flags on "
    "entities you did not edit."
)


def edited_files(transcript: str) -> set:
    """Absolute paths this session passed to Edit or Write."""
    return set(re.findall(r'"file_path"\s*:\s*"([^"]+)"', transcript))


def _confirmed(coord_id: str, accepted: set) -> bool:
    """Is there a confirm stamped at the entity's CURRENT hash?

    A fresh confirm means someone already reviewed this rot and attested
    the comments still hold. When that attestation was written does not
    matter - only whether it matches the code as it stands now.

    Skipping this check was the first version's bug: it fired on seven
    entities that already carried fresh confirms, i.e. it demanded work
    that had already been done. A gate that re-asks for completed work is
    how a check becomes noise people learn to click through.
    """
    path = os.path.join(REPO, ".coord", "meta", coord_id, "confirm.jsonl")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
    except OSError:
        return False
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        stamp = entry.get("body_hash")
        if stamp and stamp in accepted:
            return True
    return False


def rotted_in(paths: set) -> list:
    """Entities flagged comment_rot whose file was edited this session,
    excluding any already confirmed at their current hash."""
    rel = {os.path.relpath(p, REPO) for p in paths if p.startswith(REPO)}
    if not rel:
        return []
    index = os.path.join(REPO, ".coord", "index", "coordinates.json")
    try:
        with open(index, encoding="utf-8") as fh:
            entities = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for e in entities.values():
        if not (isinstance(e, dict) and e.get("comment_rot")):
            continue
        if e.get("path") not in rel:
            continue
        accepted = {h for h in (e.get("logic_hash"), e.get("body_hash")) if h}
        if _confirmed(e.get("coord_id", ""), accepted):
            continue
        out.append(f"  {e.get('qualname')}  ({e.get('path')})")
    return sorted(out)

REASON = (
    "This session used tools but never called memway_meta, so nothing it "
    "learned will survive. Before finishing:\n\n"
    "1. Ask whether this session surfaced a REASON (why code is the way it "
    "is) or a FINDING (a root cause, a defect, a surprising behavior, a "
    "constraint that stopped work, a question left open).\n"
    "2. If it did, record it with memway_meta on the entity it belongs "
    "to - one entry per reason, written for whoever reads it next.\n"
    "3. If nothing durable surfaced - a trivial edit, a lookup, a question "
    "already answered in the code - say so in one line and stop. That is a "
    "valid outcome and this check will not fire again.\n\n"
    "A refusal counts. The reason a change was declined is often the most "
    "valuable thing to record, and it is the case most often lost."
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0                      # malformed input is not the user's problem

    # Already blocked once this turn. Blocking again would loop forever.
    if payload.get("stop_hook_active"):
        return 0

    path = payload.get("transcript_path") or ""
    if not path:
        return 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            transcript = fh.read()
    except OSError:
        return 0                      # cannot verify -> do not nag

    # A session that called no tools did no work worth checking.
    used_tools = '"type":"tool_use"' in transcript.replace(" ", "")
    if not used_tools:
        return 0

    called_meta = bool(CALL_RE.search(transcript))
    rot = rotted_in(edited_files(transcript))

    problems = []
    if rot:
        problems.append(ROT_REASON.format(items="\n".join(rot)))
    if not called_meta:
        problems.append(REASON)

    # Breadcrumb: a silent hook and an unconfigured hook look identical
    # from the outside, which cost an inconclusive drill run. Values are
    # computed above rather than inlined - nesting same-type quotes inside
    # an f-string needs Python 3.12+, and this runs under whatever
    # `python3` resolves to.
    try:
        with open("/tmp/memway-hook.log", "a") as fh:
            fh.write("ran tools={} called_meta={} rot={} blocking={}\n".format(
                used_tools, called_meta, len(rot), bool(problems)))
    except OSError:
        pass
    if not problems:
        return 0
    json.dump({"decision": "block", "reason": "\n\n---\n\n".join(problems)},
              sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
