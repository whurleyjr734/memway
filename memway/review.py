"""What a change did to the MAP's knowledge, in a form a human can review.

WHY A LINE DIFF CANNOT DO THIS. The meta store is append-only JSONL, one
entry per line, and an entry is a single long JSON object. A commit that
supersedes a note shows up in git as:

    .coord/meta/C-e805a0/confirm.jsonl | 1 +

One line added, two thousand characters wide - and THE ENTRY IT
SUPERSEDES IS UNCHANGED, so it does not appear in the diff at all. The
reviewer sees an addition and has no way to see what it replaced. That is
not a formatting problem a prettier diff would solve; supersession is
positional (newest entry in a channel supersedes the ones behind it, see
metadata.for_display) and a positional relationship is invisible to a
tool that compares lines.

So this reconstructs the relationship instead of rendering the diff: for
every entry added since some revision, it pairs the new entry with the
one it supersedes and says which channel each belongs to.

APPEND-ONLY IS AN ASSUMPTION, AND IT IS CHECKED. If the old entries are
not a prefix of the new ones, somebody edited or deleted history rather
than adding to it. That is reported as `rewritten`, loudly, because the
whole staleness model rests on old entries staying put.

Read-only: this shells out to `git show` and touches nothing.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .metadata import CHANNELS
from .payload import rank_bound_report


def _git(repo: Path, *args) -> tuple[int, str]:
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout


def _entries(text: str) -> list:
    out = []
    for line in text.splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                # A damaged line is not the whole file - the same posture
                # replay.py takes when reading a bundle.
                continue
    return out


def _meta_files(repo: Path) -> list:
    root = repo / ".coord" / "meta"
    if not root.is_dir():
        return []
    return sorted(f for f in root.glob("*/*.jsonl") if f.stem in CHANNELS)


def review(repo: str, since: str = "HEAD") -> dict:
    """Knowledge added since `since`, each entry paired with what it replaced.

    `since` is any git revision. The comparison is against the WORKING
    TREE, so this answers "what am I about to ask someone to review".
    """
    repo_p = Path(repo).resolve()
    rc, _ = _git(repo_p, "rev-parse", "--git-dir")
    if rc != 0:
        return {"error": f"{repo_p} is not a git repository"}
    rc, _ = _git(repo_p, "rev-parse", "--verify", "--quiet", since)
    if rc != 0:
        return {"error": f"unknown revision {since!r}"}

    added, rewritten = [], []
    for f in _meta_files(repo_p):
        rel = f.relative_to(repo_p).as_posix()
        coord, channel = f.parent.name, f.stem
        now = _entries(f.read_text())
        rc, old_text = _git(repo_p, "show", f"{since}:{rel}")
        was = _entries(old_text) if rc == 0 else []

        # APPEND-ONLY, VERIFIED. Comparing texts rather than counts: a
        # rewritten history has the same length as often as not.
        prefix_ok = all(a.get("text") == b.get("text")
                        for a, b in zip(was, now))
        if not prefix_ok or len(now) < len(was):
            rewritten.append({"coordinate": coord, "channel": channel,
                              "was": len(was), "now": len(now)})
            continue
        if len(now) == len(was):
            continue

        # The entry that was newest before this change is the one the
        # first new entry supersedes; each new entry supersedes the one
        # before it.
        #
        # A RE-STAMP SUPERSEDES NOTHING AND IS SUPERSEDED BY NOTHING. It
        # carries no claim (metadata.MetaStore.reaffirm), so it is not a
        # position in this chain - and reporting it as one was wrong in
        # three ways at once on the surface where a human judges the
        # change: it rendered as `+ ` with no text, it announced that an
        # empty entry had REPLACED the note it was actually vouching for,
        # and by sitting in `previous` it made the next real entry read
        # "first entry in this channel". Same rule as for_display, which
        # the reading order learned first.
        previous = next((e for e in reversed(was)
                         if not e.get("reaffirms")), None)
        for en in now[len(was):]:
            if en.get("reaffirms"):
                added.append({
                    "coordinate": coord,
                    "channel": channel,
                    "author": en.get("author", ""),
                    "text": "",
                    "reaffirms": en["reaffirms"],
                    "supersedes": None,
                    "supersedes_author": None,
                    "replaces": None,
                })
                continue                      # does NOT become `previous`
            added.append({
                "coordinate": coord,
                "channel": channel,
                "author": en.get("author", ""),
                "text": en.get("text", ""),
                "supersedes": (previous.get("text") if previous else None),
                "supersedes_author": (previous.get("author")
                                      if previous else None),
                # the REASON the belief changed, when the author gave one
                "replaces": en.get("replaces") or None,
            })
            previous = en

    by_channel: dict = {}
    for a in added:
        by_channel[a["channel"]] = by_channel.get(a["channel"], 0) + 1
    shown, report = rank_bound_report(
        added, "added",
        rank=lambda a: (a["supersedes"] is None, a["coordinate"], a["channel"]))
    return {
        "since": since,
        "coordinates_touched": len({a["coordinate"] for a in added}),
        "added": shown,
        **report,
        "added_by_channel": by_channel,
        "superseding": sum(1 for a in added if a["supersedes"]),
        # not knowledge added - see render()
        "reaffirmed": sum(1 for a in added if a.get("reaffirms")),
        # Never silently dropped: a rewrite breaks the staleness model.
        "rewritten": rewritten,
        "note": ("a line diff cannot show supersession - the entry being "
                 "superseded is unchanged on disk, so it never appears in "
                 "the diff. Each item below pairs the new entry with what "
                 "it replaced."),
    }


def render(result: dict, width: int = 78) -> str:
    """The same thing as text, for a terminal or a PR comment."""
    if result.get("error"):
        return f"error: {result['error']}"
    lines = []
    n, tot = result["added_shown"], result["added_total"]
    lines.append(f"knowledge added since {result['since']}: {tot} "
                 f"{'entry' if tot == 1 else 'entries'} across "
                 f"{result['coordinates_touched']} coordinate(s)"
                 + (f" (showing {n})" if n < tot else ""))
    if result["added_by_channel"]:
        lines.append("  " + ", ".join(f"{k} {v}" for k, v in
                                      sorted(result["added_by_channel"].items())))
    # Counted separately because a re-stamp is not knowledge added. Saying
    # "6 entries" over four stamps and two claims overstates what there is
    # to review by three times.
    if result.get("reaffirmed"):
        n = result["reaffirmed"]
        lines.append(f"  of which {n} "
                     f"{'is a re-stamp' if n == 1 else 'are re-stamps'} - "
                     f"existing entries re-read and left standing")
    if result["rewritten"]:
        lines.append("")
        lines.append("  REWRITTEN HISTORY - append-only was violated:")
        for r in result["rewritten"]:
            lines.append(f"    {r['coordinate']} [{r['channel']}] "
                         f"{r['was']} -> {r['now']} entries")
    def clip(s, n=width - 6):
        s = " ".join((s or "").split())
        return s if len(s) <= n else s[:n - 1] + "…"
    for a in result["added"]:
        lines.append("")
        lines.append(f"  {a['coordinate']} [{a['channel']}]"
                     + (f" by {a['author']}" if a['author'] else ""))
        if a.get("reaffirms"):
            # A re-stamp is a change worth seeing in review - somebody
            # re-read this and let it stand - but it is not an addition
            # and it replaces nothing.
            n = a["reaffirms"]
            lines.append(f"    ↻ re-stamped {n} existing "
                         f"{'entry' if n == 1 else 'entries'} at the "
                         f"current hash - no new claim")
            continue
        lines.append(f"    + {clip(a['text'])}")
        if a["supersedes"]:
            lines.append(f"    ↳ supersedes: {clip(a['supersedes'])}")
            if a.get("replaces"):
                lines.append(f"      because: {clip(a['replaces'])}")
        else:
            lines.append("    ↳ first entry in this channel")
    return "\n".join(lines)


def search(repo: str, query: str, channel: str = "", limit: int = 12) -> dict:
    """Which coordinates hold knowledge mentioning `query`.

    THE MISSING DIRECTION. Every other read starts from a coordinate:
    you know what you are looking at and ask what is known about it. An
    agent opening a task has the opposite problem - it knows a SUBJECT
    ("proxies", "timeout", "retry") and no idea which coordinates were
    ever taught anything about it. Without this the only way to find
    prior reasoning is to already know where it lives, which means
    accumulated knowledge is stored but not findable, and the same
    ground gets re-derived.

    Case-insensitive substring across channels. Deliberately not fuzzy
    and not ranked by relevance: this is grep, and a grep that guesses is
    harder to trust than one that does exactly what it says. Matches are
    ordered by how many entries a coordinate has on the subject, which is
    a fact rather than a judgement.

    SUPERSEDED ENTRIES ARE SEARCHED AND LABELLED, not skipped. A note
    somebody replaced is often exactly what you want when asking "was
    this considered before" - but it must arrive marked as history.
    """
    from .query import _ctx
    from .metadata import for_display, accepted_for

    q = (query or "").strip().lower()
    if not q:
        return {"error": "empty query"}
    if channel and channel not in CHANNELS:
        return {"error": f"unknown channel {channel!r}; "
                         f"expected one of {sorted(CHANNELS)}"}
    ctx = _ctx(repo)
    if not ctx:
        return {"error": f"no index at {repo}; run memway init first"}
    _, _coord, ix, _edges, meta = ctx

    hits = []
    for cid, ent in ix.entities.items():
        rows = for_display(meta.read_all(cid, current_hash=accepted_for(ent)))
        found = []
        for r in rows:
            if channel and r["channel"] != channel:
                continue
            text = r.get("text", "")
            if q in text.lower():
                found.append({
                    "channel": r["channel"],
                    "stale": bool(r.get("stale")),
                    "superseded": bool(r.get("superseded")),
                    "author": r.get("author", ""),
                    "excerpt": _excerpt(text, q),
                })
        if found:
            hits.append({
                "coordinate": cid,
                "qualname": ent.qualname,
                "kind": ent.kind,
                "path": ent.path,
                "matches": len(found),
                "live": sum(1 for f in found if not f["superseded"]),
                "entries": found[:3],
            })
    shown, report = rank_bound_report(
        hits, "hits", rank=lambda h: (-h["live"], -h["matches"], h["qualname"]),
        cap=limit)
    return {"query": query, "channel": channel or "all",
            "hits": shown, **report,
            "note": ("superseded entries are included and marked - prior "
                     "reasoning somebody replaced is often what you want "
                     "when asking whether something was considered before")}


def _excerpt(text: str, q: str, width: int = 160) -> str:
    """The matched sentence, not the first N characters of the entry."""
    flat = " ".join(text.split())
    i = flat.lower().find(q)
    if i < 0:
        return flat[:width]
    start = max(0, i - width // 3)
    end = min(len(flat), i + width - (i - start))
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")
