"""
Evidence: the DERIVED half of excavated knowledge.

THE SPLIT THIS MODULE EXISTS TO ENFORCE
=======================================

Excavation produces two things that look similar and must never be
confused:

  EVIDENCE   what the record SAYS. A commit body, a PR description.
             Structured, regenerable from git/the forge, never precious.
             If it is lost, one re-dig restores it byte for byte.

  VERDICT    what a READER CONCLUDED. "this dot-handling is a security
             control, do not simplify it." Irreplaceable: it is judgment,
             and no amount of re-fetching reproduces it.

v2's excavate wrote both into the same channel as one blob of text, which
made the derived half unclearable without risking the authored half and
duplicated every commit body into the map. Here they are separate by
CONSTRUCTION, not by care:

  evidence  ->  .coord/evidence/<coord_id>.jsonl   (derived, gitignored)
  verdict   ->  .coord/meta/<coord_id>/notes.jsonl (authored, committed)

`clear()` removes the evidence DIRECTORY. It cannot touch meta/ because
it never addresses meta/ - the paths are siblings, not parent and child.
A test asserts that across every channel.

A verdict POINTS at evidence and does not restate it:

    VERDICT 69065ca869: this is a security control, not defensive style

Body text lives in exactly one place. If the evidence is cleared, the
verdict still renders - annotated as uncached, never broken, never
silently degraded.

TWO-AXIS STALENESS
==================

The two halves go stale for different reasons and are checked separately:

  a VERDICT is stamped with the entity's logic hash, so it goes stale
  when the CODE changes (existing machinery, untouched);

  EVIDENCE carries `dug_through_sha`, so it goes stale when HISTORY
  grows - new commits touched the range since the last dig.

Neither implies the other. Code can change with no new commits touching
it (a rename above it), and history can grow without the entity's logic
moving (a comment fix). Collapsing them into one flag would make both
lie.
"""

import json
import re
import shutil
import time
from pathlib import Path

EVIDENCE_DIR = "evidence"

# "VERDICT <ref>: <judgment>" - ref is a sha (any length) or a PR number
# written #123. Deliberately narrow: a note that merely mentions the word
# verdict is a free-text note and must stay one.
# Two ref shapes, deliberately distinct: a forge ref is #N (PR numbers
# are small - #11 is legitimate), a commit ref is 7-40 hex. A single
# loose pattern either rejected real PR numbers or matched any short
# word after "VERDICT".
VERDICT_RE = re.compile(
    r"^VERDICT\s+(#\d{1,7}|[0-9a-fA-F]{7,40})\s*:\s*(.+)", re.DOTALL)

UNCACHED_NOTE = "(evidence not cached - re-dig to restore)"


def evidence_root(coord) -> Path:
    return Path(coord) / EVIDENCE_DIR


def _path(coord, coord_id: str) -> Path:
    return evidence_root(coord) / f"{coord_id}.jsonl"


def read(coord, coord_id: str) -> list:
    """Every evidence record for one coordinate, newest first."""
    p = _path(coord, coord_id)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue                    # a damaged line is not an error here
    out.sort(key=lambda r: r.get("date", ""), reverse=True)
    return out


def dug_through(coord, coord_id: str) -> str:
    """The HEAD sha the last dig covered, or '' if never dug."""
    recs = read(coord, coord_id)
    return recs[0].get("dug_through_sha", "") if recs else ""


def known_refs(coord, coord_id: str) -> set:
    """Every ref already stored, so a delta fetch can skip them."""
    out = set()
    for r in read(coord, coord_id):
        if r.get("sha"):
            out.add(r["sha"])
        if r.get("number") is not None:
            out.add(f"#{r['number']}")
    return out


def index_by_ref(records: list) -> dict:
    """{ref -> record} for verdict lookup.

    A commit is addressable by full sha OR any prefix a human would
    write, because a verdict says `VERDICT 69065ca869:` and the record
    holds the full 40-char sha.
    """
    ix = {}
    for r in records:
        if r.get("source") == "pr" and r.get("number") is not None:
            ix[f"#{r['number']}"] = r
            ix[str(r["number"])] = r
        sha = r.get("sha") or ""
        if sha:
            ix[sha] = r
            for n in (7, 8, 9, 10, 12):
                ix[sha[:n]] = r
    return ix


def from_dig(payload: dict, head_sha: str) -> list:
    """Turn a dig payload into evidence records.

    Commits and PR bodies both become records: a PR body is evidence in
    exactly the same sense as a commit body, and storing it here is what
    keeps it out of the authored channel.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = []
    for c in payload.get("candidates", []):
        out.append({
            "source": "commit",
            "sha": c.get("sha", ""),
            "short_sha": c.get("short_sha", ""),
            "date": c.get("date", ""),
            "author": c.get("author", ""),
            "subject": c.get("subject", ""),
            "body": c.get("body", ""),
            "provenance_label": c.get("provenance", ""),
            "released_in": c.get("released_in", []),
            # the ref LIST, bodies excluded - an unresolved ref is still
            # information (the number is there, it just was not fetched),
            # and dropping it made a cached dig quietly thinner than a
            # live one. Bodies stay in their own records, stored once.
            "pr_refs": [{"number": r.get("number"),
                         "unavailable_reason": r.get("unavailable_reason")}
                        for r in c.get("pr_refs", [])],
            "fetched_at": now,
            "dug_through_sha": head_sha,
        })
        for r in c.get("pr_refs", []):
            if not r.get("body"):
                continue            # unresolved refs are not evidence
            out.append({
                "source": "pr",
                "number": r.get("number"),
                "sha": "",
                # which commit carried this reference: without it a
                # cached re-dig cannot put the PR back on its commit and
                # silently returns thinner candidates than the live dig.
                "via_sha": c.get("sha", ""),
                "date": c.get("date", ""),
                "author": c.get("author", ""),
                "subject": f"PR #{r.get('number')} (via {c.get('short_sha')})",
                "body": r.get("body", ""),
                "provenance_label": c.get("provenance", ""),
                "released_in": c.get("released_in", []),
                "fetched_at": now,
                "dug_through_sha": head_sha,
            })
    return out


def write(coord, coord_id: str, records: list, head_sha: str) -> dict:
    """Merge records in, newest dug_through_sha wins. Returns a receipt."""
    root = evidence_root(coord)
    root.mkdir(parents=True, exist_ok=True)
    existing = read(coord, coord_id)
    seen = {}
    for r in existing + records:
        key = (r.get("source"), r.get("sha") or "", r.get("number"))
        seen[key] = r
    merged = list(seen.values())
    # every record carries the sha this coordinate is now dug through, so
    # the marker survives even if the newest commit is later cleared
    for r in merged:
        r["dug_through_sha"] = head_sha or r.get("dug_through_sha", "")
    merged.sort(key=lambda r: r.get("date", ""), reverse=True)
    _path(coord, coord_id).write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in merged))
    return {"stored": len(merged), "added": len(merged) - len(existing),
            "dug_through_sha": head_sha}


def clear(coord) -> dict:
    """Delete the evidence store and NOTHING else.

    Addresses .coord/evidence only. meta/ is a sibling directory, so
    there is no path by which this can reach authored knowledge - that is
    the point of the layout.
    """
    root = evidence_root(coord)
    if not root.exists():
        return {"cleared": 0, "coordinates": 0}
    files = list(root.glob("*.jsonl"))
    n = sum(len([l for l in f.read_text().splitlines() if l.strip()])
            for f in files)
    shutil.rmtree(root)
    return {"cleared": n, "coordinates": len(files)}


def parse_verdict(text: str):
    """(ref, judgment) if this note is a verdict, else None."""
    m = VERDICT_RE.match((text or "").strip())
    if not m:
        return None
    return m.group(1), m.group(2).strip()


def decorate_knowledge(entries: list, records: list) -> list:
    """Join verdicts to their evidence for the read surface.

    A verdict whose evidence is present renders WITH the source subject
    and sha inline, so the reader gets the author's own words and the
    judgment without either being duplicated in the store. A verdict
    whose evidence was cleared renders alone and says so - never broken,
    never silently degraded.
    """
    ix = index_by_ref(records)
    for en in entries:
        v = parse_verdict(en.get("text", ""))
        if not v:
            continue
        ref, judgment = v
        en["verdict"] = {"ref": ref, "judgment": judgment}
        rec = ix.get(ref) or ix.get(ref.lstrip("#"))
        if rec is None:
            en["verdict"]["evidence"] = None
            en["verdict"]["note"] = UNCACHED_NOTE
        else:
            en["verdict"]["evidence"] = {
                "source": rec.get("source"),
                "ref": rec.get("short_sha") or f"#{rec.get('number')}",
                "date": rec.get("date", ""),
                "author": rec.get("author", ""),
                "subject": rec.get("subject", ""),
                "released_in": rec.get("released_in", []),
            }
    return entries


def summarise(records: list, top: int = 5) -> dict:
    """The read-surface section: counts plus the most recent items.

    Subjects and refs only. Bodies are deliberately withheld - they are
    the bulk, and the reader asks for them by ref (`memway evidence`) when
    a subject earns the attention.
    """
    return {
        "count": len(records),
        "commits": sum(1 for r in records if r.get("source") == "commit"),
        "prs": sum(1 for r in records if r.get("source") == "pr"),
        "dug_through_sha": records[0].get("dug_through_sha", "") if records
                           else "",
        "top": [{
            "source": r.get("source"),
            "ref": r.get("short_sha") or f"#{r.get('number')}",
            "date": r.get("date", ""),
            "subject": r.get("subject", ""),
            "provenance_label": r.get("provenance_label", ""),
        } for r in records[:top]],
        "hint": "bodies are stored once, here - fetch one with "
                "`memway evidence <repo> <ref>`",
    }
