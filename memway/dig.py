"""
Dig: demand-paged history retrieval for ONE entity.

MECHANICS ONLY. This module resolves an entity through the map, walks the
history of exactly its line range, labels provenance, follows forge
references, and reconciles release tags. It returns CANDIDATES.

THE FENCE
=========

  It never gates.   Every commit the range touched comes back.
  It never scores.  No ranking, no rationale/restatement verdict.
  It never writes.  Nothing under .coord is opened for writing, ever.

Judging rationale vs restatement, and writing keepers back to the map,
belong to the SESSION AGENT - the caller knows what it is about to do and
the tool does not. That is not a layering nicety; it is the measured
conclusion from memway-tasks #9. Structural scoring was taken to its
ceiling in the batch gate and stopped at a semantic boundary, and the
judgment that mattered in the matplotlib run ("reverting the epsilon
reintroduces two bugs") depended on knowing the caller's intent.

A test asserts .coord is byte-identical across a dig. Keep it that way.

WHY EACH STEP EXISTS
====================

D-D1  Resolve through the MAP, use its exact line range.
      This is the load-bearing map contribution. `git log -L` needs a
      range; the map already has one, exactly, without reading the file.

D-D2  LINE-RANGE form, never `:funcname:`.
      The funcname form is .gitattributes-dependent and fails outright on
      real repos - measured on Django:
          fatal: -L parameter '_order_by_pairs' starting at line 1: no match
      because django sets no `*.py diff=python`, so git's default funcname
      regex does not match an indented `def`. Forcing the driver makes it
      work and returns the SAME commits, so the line-range form loses
      nothing and depends on nothing.

D-D3  Label pre-extraction history.
      `-L` follows the REGION, not the entity, so it walks back through
      the code's earlier homes. On Django, 18 of 30 commits predate
      `_order_by_pairs` (created 0461b7a6b6); the oldest is 2009
      multiple-database support, which is not this function in any
      meaningful sense. Range drift is lineage in a git costume. The tool
      marks the boundary rather than letting the caller infer it.

D-D4  Follow forge references (#NNNN).
      On a forge-centric repo this is the difference between a usable dig
      and an empty one. Measured on matplotlib #32186: 12 commit messages,
      2 carried rationale, NEITHER decisive - and both commits that
      actually changed behaviour had EMPTY bodies. The entire causal
      explanation lived in the PR description. A dig that stops at
      `git log` stops one hop short.

      The forge leg NEVER fails the dig: gh missing, unauthenticated, or a
      non-GitHub remote all degrade to candidates-with-reason.

D-D5  Reconcile release branches (the tag-containment doctrine).
      `git tag --contains <main sha>` reports "unreleased" for a change
      that shipped under a BACKPORT sha. Measured on matplotlib: the main
      sha 5a4af86f94 is in no tag, while its backport c624aec7de is in
      v3.11.1. Reporting the main sha as the cause of a released
      regression is confidently wrong, which is worse than silent - so an
      empty tag list carries an explicit warning.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

# The tool's own name for "no forge answer, and here is why".
FORGE_NO_GH = "gh-not-installed"
FORGE_UNAUTH = "gh-not-authenticated"
FORGE_NOT_GITHUB = "remote-is-not-github"
FORGE_FETCH_FAILED = "forge-fetch-failed"
FORGE_NO_REMOTE = "no-git-remote"

BACKPORT_WARNING = ("absent from all tags - may be released under a "
                    "backported sha; verify on release branches")

REGION_HISTORY = "region-history (predates this entity)"
ENTITY_HISTORY = "entity-history"
# The entity is older than the range's own recorded history: every
# candidate is entity history, and that is PROVEN, not merely unfound.
PREDATES_RANGE = "entity-predates-recorded-range-history"

_ISSUE_REF = re.compile(r"#(\d{1,7})\b")

# Which #NNNN are actually GitHub PRs, and which are some other tracker's
# ticket numbers that merely look identical.
#
# MEASURED FAILURE (Django answer key): Django's messages carry TRAC ticket
# numbers - "Fixed #1142 -- Added multiple database support." - and
# django/django on GitHub also has a PR #1142, about Urdu RTL locales.
# Fetching by bare number attached four unrelated PR bodies to four
# commits. That is worse than unavailable: it is confidently wrong, the
# same failure class the backport warning exists to prevent.
#
# So fetch only refs in a GitHub-shaped position: the squash-merge
# trailer "subject (#NNNN)", an explicit "PR #NNNN", or a merge commit.
# Everything else is still REPORTED - the caller sees the number and can
# look it up - but is not resolved against the wrong tracker.
_GH_REF = re.compile(r"\(#(\d{1,7})\)|\bPR #(\d{1,7})\b|"
                     r"\bMerge pull request #(\d{1,7})\b")
AMBIGUOUS_REF = ("ref is not in a GitHub-PR position (looks like another "
                 "tracker's ticket number); not fetched to avoid attaching "
                 "an unrelated PR body")
_REC, _FLD = "\x1e", "\x1f"

# MCP payload ceiling. Finding #41: a single PR body can be tens of KB and
# a dig can carry dozens. The CLI --json path is UNCAPPED - a file on disk
# has no context window.
MCP_CAP_BYTES = 60_000
_TRUNC = "\n[...truncated by memway_dig payload cap...]"


def _git(repo: Path, *args, timeout=120) -> tuple:
    """(stdout, ok). Never raises; a dead git is a degraded dig, not a
    crash."""
    try:
        p = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return "", False
    return p.stdout, p.returncode == 0


def _resolve(repo: Path, ref: str):
    """Entity via the map (D-D1). Returns (entity, error_dict)."""
    from .indexer import Indexer
    coord = repo / ".coord"
    if not (coord / "index" / "coordinates.json").exists():
        return None, {"error": f"no map at {repo}/.coord - run `memway init` first"}
    ix = Indexer(repo, coord)
    # write_cache=False is THE FENCE, not an optimisation: load_existing
    # otherwise warms .coord/cache/coordinates.pkl, which is a write, and
    # a dig that writes cannot claim to be a read.
    ix.load_existing(write_cache=False)
    e = ix.resolve(ref)
    if e is None:
        from difflib import SequenceMatcher
        near = sorted(ix.by_qualname,
                      key=lambda q: SequenceMatcher(None, str(ref), q).ratio(),
                      reverse=True)[:3]
        return None, {"error": f"{ref!r} does not resolve in the map",
                      "closest": near}
    return e, None


def _log_range(repo: Path, path: str, start: int, end: int) -> list:
    """git log -L<start>,<end>:<file> (D-D2). Newest first."""
    fmt = _FLD.join(["%H", "%h", "%aI", "%an", "%s", "%b"]) + _REC
    out, ok = _git(repo, "log", f"-L{start},{end}:{path}", "-s",
                   f"--format={fmt}")
    if not ok:
        return []
    rows = []
    for rec in out.split(_REC):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        parts = rec.split(_FLD)
        if len(parts) < 6:
            continue
        sha, short, date, author, subject, body = parts[:6]
        rows.append({"sha": sha, "short_sha": short, "date": date[:10],
                     "author": author, "subject": subject,
                     "body": body.strip()})
    return rows


def _creation_boundary(repo: Path, path: str, short_name: str, shas: list):
    """The commit where the entity's own definition first appears (D-D3).

    `-S<name>` finds commits that changed the number of occurrences of the
    name; the OLDEST such commit in this file introduced it. Restricted to
    shas the range actually touched, so an unrelated mention elsewhere in
    the file cannot move the boundary.
    """
    if not short_name:
        return None
    # --follow is required, not optional: without it the search stops at
    # the last file RENAME rather than the entity's creation. Measured on
    # memway's own map - the coordsys->memway rename hid the boundary and
    # every candidate came back mislabelled as entity-history.
    out, ok = _git(repo, "log", "--follow", "--reverse", "--format=%H",
                   f"-S{short_name}", "--", path)
    if not ok:
        return None
    known = set(shas)
    order = [s.strip() for s in out.splitlines() if s.strip()]
    for sha in order:
        if sha in known:
            return sha
    # The entity's creation is OLDER than anything the range recorded -
    # measured on matplotlib's get_width_height, introduced 2005-06-18
    # while the range's own history starts 2005-07-22. That is provable,
    # not unknowable: if the creation commit is an ancestor of the oldest
    # candidate, every candidate is entity history and none is region
    # history. Saying "unverified" here would overclaim uncertainty.
    if order and shas:
        _, ok = _git(repo, "merge-base", "--is-ancestor", order[0], shas[-1])
        if ok:
            return PREDATES_RANGE
    return None


def _label_provenance(cands: list, boundary: str | None) -> None:
    """Everything OLDER than the boundary is region history (D-D3).

    `cands` is newest-first, so 'older' is 'after the boundary index'.
    The boundary commit itself is entity history - it created the entity.
    """
    if boundary is None:
        for c in cands:
            c["provenance"] = ENTITY_HISTORY
        return
    idx = next((i for i, c in enumerate(cands) if c["sha"] == boundary), None)
    for i, c in enumerate(cands):
        c["provenance"] = (REGION_HISTORY if idx is not None and i > idx
                           else ENTITY_HISTORY)


def _github_slug(repo: Path):
    """(owner/repo, None) or (None, reason)."""
    out, ok = _git(repo, "remote", "get-url", "origin")
    url = out.strip()
    if not ok or not url:
        return None, FORGE_NO_REMOTE
    m = re.search(r"github\.com[:/]+([^/]+/[^/\s]+?)(?:\.git)?/?$", url)
    if not m:
        return None, FORGE_NOT_GITHUB
    return m.group(1), None


def _gh_ready(repo: Path):
    """(slug, None) when the forge leg can run, else (None, reason)."""
    if shutil.which("gh") is None:
        return None, FORGE_NO_GH
    slug, why = _github_slug(repo)
    if slug is None:
        return None, why
    try:
        p = subprocess.run(["gh", "auth", "status"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None, FORGE_UNAUTH
    if p.returncode != 0:
        return None, FORGE_UNAUTH
    return slug, None


def _fetch_pr(slug: str, number: str, cache: dict):
    """PR body via gh, memoised. (body, None) or (None, reason)."""
    if number in cache:
        return cache[number]
    try:
        p = subprocess.run(
            ["gh", "pr", "view", number, "--repo", slug, "--json", "body"],
            capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            got = (None, FORGE_FETCH_FAILED)
        else:
            got = (json.loads(p.stdout).get("body") or "", None)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        got = (None, FORGE_FETCH_FAILED)
    cache[number] = got
    return got


def _forge_refs(cands: list, repo: Path, forge: bool) -> None:
    """Attach pr_refs to each candidate (D-D4). Never raises."""
    slug, reason = (_gh_ready(repo) if forge else (None, "forge-leg-disabled"))
    cache: dict = {}
    for c in cands:
        blob = f"{c['subject']}\n{c['body']}"
        gh_shaped = {n for grp in _GH_REF.findall(blob) for n in grp if n}
        nums, seen = [], set()
        for n in _ISSUE_REF.findall(blob):
            if n not in seen:
                seen.add(n)
                nums.append(n)
        refs = []
        for n in nums:
            if n not in gh_shaped:
                refs.append({"number": int(n), "body": None,
                             "unavailable_reason": AMBIGUOUS_REF})
                continue
            if slug is None:
                refs.append({"number": int(n), "body": None,
                             "unavailable_reason": reason})
                continue
            body, why = _fetch_pr(slug, n, cache)
            refs.append({"number": int(n), "body": body,
                         "unavailable_reason": why})
        c["pr_refs"] = refs


def _released_in(repo: Path, cands: list) -> None:
    """Tag containment + the backport warning (D-D5)."""
    for c in cands:
        out, ok = _git(repo, "tag", "--contains", c["sha"])
        tags = [t.strip() for t in out.splitlines() if t.strip()] if ok else []
        c["released_in"] = tags
        c.setdefault("warnings", [])
        if not tags:
            c["warnings"].append(BACKPORT_WARNING)


def _apply_cap(payload: dict, cap: int) -> dict:
    """Trim to a byte ceiling, marking every cut (finding #41).

    Bodies are trimmed before candidates are dropped: a truncated body
    still names its commit, while a dropped candidate is invisible.
    """
    def size(p):
        return len(json.dumps(p).encode())

    if size(payload) <= cap:
        return payload
    cands = payload["candidates"]
    for c in cands:                       # 1. PR bodies are the big ones
        for r in c.get("pr_refs", []):
            if r.get("body") and len(r["body"]) > 400:
                r["body"] = r["body"][:400] + _TRUNC
                r["truncated"] = True
        if size(payload) <= cap:
            return payload
    for c in cands:                       # 2. then commit bodies
        if len(c.get("body", "")) > 400:
            c["body"] = c["body"][:400] + _TRUNC
            c["truncated"] = True
        if size(payload) <= cap:
            return payload
    while len(cands) > 1 and size(payload) > cap:   # 3. drop, and say so
        cands.pop()
    payload["payload_capped"] = {
        "cap_bytes": cap,
        "candidates_returned": len(cands),
        "note": "payload exceeded the MCP cap; oldest candidates dropped. "
                "Use the CLI (`memway dig <repo> <ref> --json`) for the "
                "uncapped payload.",
    }
    return payload


def dig(repo: str, ref: str, *, cap_bytes: int | None = None,
        forge: bool = True) -> dict:
    """Return history candidates for one entity. Judgment is the caller's.

    Writes nothing. Reads .coord only to resolve `ref`.
    """
    repo_p = Path(repo).resolve()
    e, err = _resolve(repo_p, ref)
    if err is not None:
        return err

    start = int(getattr(e, "lineno", 0) or 0)
    end = int(getattr(e, "end_lineno", 0) or 0) or start
    notes = []
    if end < start:
        start, end = end, start
    cands = _log_range(repo_p, e.path, start, end)
    if not cands:
        notes.append("git log -L returned nothing - the path may be "
                     "untracked, or the range may be outside the file at HEAD")

    short_name = e.qualname.rsplit(".", 1)[-1]
    boundary = _creation_boundary(repo_p, e.path, short_name,
                                  [c["sha"] for c in cands])
    _label_provenance(cands, None if boundary == PREDATES_RANGE else boundary)
    if boundary == PREDATES_RANGE and cands:
        notes.append("the entity predates the oldest commit this range "
                     "records, so every candidate is entity-history (proven "
                     "by ancestry, not assumed)")
    elif boundary is None and cands:
        notes.append("creation boundary not found; all candidates labelled "
                     "entity-history (provenance is unverified, not proven)")
    for c in cands:
        c.setdefault("warnings", [])
    _released_in(repo_p, cands)
    _forge_refs(cands, repo_p, forge)

    payload = {
        "entity": {"coord_id": e.coord_id, "qualname": e.qualname,
                   "path": e.path, "lineno": start, "end_lineno": end},
        "dig": {"command": f"git log -L{start},{end}:{e.path}",
                "form": "line-range (never :funcname: - see D-D2)",
                "creation_boundary": boundary},
        "candidates": cands,
        "counts": {
            "total": len(cands),
            "entity_history": sum(1 for c in cands
                                  if c["provenance"] == ENTITY_HISTORY),
            "region_history": sum(1 for c in cands
                                  if c["provenance"] == REGION_HISTORY),
        },
        "contract": "candidates only - judging rationale vs restatement, "
                    "and writing anything back to the map, is the caller's "
                    "job. This tool never gates, scores, or writes.",
        "notes": notes,
    }
    if cap_bytes:
        payload = _apply_cap(payload, cap_bytes)
    return payload
