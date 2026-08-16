"""Map freshness: does the map still describe the tree it claims to?

THE PROBLEM THIS SOLVES, MEASURED ON THIS REPO
==============================================

memway's own map sat seven commits behind HEAD (652f58d while HEAD was
f2e6bc3), across three commits that changed parsing, hashing and entity
extraction. The workflow rules say to re-index when those move. The rule
was written down, by the author, on the flagship repo, and it still went
unnoticed - because nothing reported it. The resync produced +91 entities
and a rename the map had never seen.

Worse than the lag was its costume: the attention queue showed stale
comment-rot, and a reader cannot tell "your comment drifted" from "your
map is old". The second wears the clothes of the first.

THREE LAYERS, AND ONLY ONE OF THEM IS A GUARANTEE
=================================================

  hooks       automate the common case (commit, checkout, merge)
  --if-stale  the same check for scripts and CI
  the WARNING every read tool emits when the map lags HEAD

Hooks cannot fire during a bisect, in a fresh worktree, on a hand-edited
tree, or in any environment that never ran `memway hooks install`. So the
warning is the actual answer to the class: the map may lag, but it must
never lag SILENTLY. Automation is a convenience layered on top of that
promise, not a substitute for it.

RECORDING IS ADDITIVE. The indexed-at sha goes into .coord/manifest.json
alongside the existing keys. A map written by an older memway simply has
no sha recorded, and reports "unknown" rather than lying in either
direction.
"""

import hashlib
import json
import subprocess
from pathlib import Path

SHA_KEY = "indexed_at_sha"
TREE_KEY = "indexed_at_tree"
DIRTY_KEY = "indexed_at_dirty"


def _git(repo, *args) -> tuple:
    """(stdout, ok). Never raises: no git, no repo, no problem."""
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip(), r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return "", False


def head_sha(repo) -> str:
    """HEAD, or '' when this is not a git repo (which is not an error)."""
    out, ok = _git(repo, "rev-parse", "HEAD")
    return out if ok else ""


def is_dirty(repo) -> bool:
    """Tracked SOURCE modified against HEAD. Excludes .coord itself.

    Two exclusions, both load-bearing:

    Untracked files are not dirt. A scratch file beside the source does
    not change what the map describes.

    `.coord` is not dirt, and this one is not a nicety. memway tells you
    to COMMIT the map, so indexing modifies a tracked path by definition.
    Counting that made --if-stale see a dirty tree immediately after a
    successful index, so it reindexed, which dirtied the tree, forever.
    Measured on a fixture before the exclusion went in.
    """
    _, ok = _git(repo, "diff", "--quiet", "HEAD", "--",
                 ".", ":(exclude).coord")
    return not ok


def code_tree(repo) -> str:
    """A hash of the STAGED code, excluding .coord. '' when unavailable.

    THE TREE, NOT THE COMMIT. Freshness asked "is the map's sha the same
    as HEAD" and then patched around the consequences: committing the map
    moves HEAD, so 0.53.2 had to add a commit-counting rule that excludes
    .coord just to stop the warning firing forever on the workflow this
    project recommends. Comparing CONTENT instead makes that whole class
    disappear - same code, same map, current, wherever git wandered. It
    also makes bisect, rebase and fresh worktrees honest for free: they
    change commit shas and leave the tree alone.

    WHY NOT `git write-tree`, which is the obvious answer: write-tree
    covers .coord, and the value gets recorded INTO .coord/manifest.json.
    Recording the hash would change the tree the hash describes, so it
    could never match on the next read. Excluding .coord is not a nicety
    here, it is what makes the scheme self-consistent - measured before
    the design was committed to.

    `ls-files -s` is the INDEX, deliberately. During a pre-commit hook the
    index is exactly what is about to be committed, so a hash taken there
    matches HEAD's tree the instant the commit lands - which is what lets
    the map ride inside its own commit and still read as current.
    """
    out, ok = _git(repo, "ls-files", "-s", "--", ".", ":(exclude).coord")
    if not ok:
        return ""
    return hashlib.sha256(out.encode()).hexdigest()[:16]


def head_tree(repo) -> str:
    """The same hash for HEAD's committed content. '' when unavailable."""
    out, ok = _git(repo, "ls-tree", "-r", "HEAD", "--", ".")
    if not ok:
        return ""
    lines = [l for l in out.splitlines()
             if l.strip() and "\t.coord/" not in l and not l.endswith("\t.coord")]
    norm = []
    for l in lines:
        meta, _, path = l.partition("\t")
        mode, _, rest = meta.partition(" ")
        _, _, blob = rest.partition(" ")
        norm.append(f"{mode} {blob} 0\t{path}")
    return hashlib.sha256("\n".join(norm).encode()).hexdigest()[:16] if norm else ""


def code_commits_between(repo, was: str, sha: str) -> tuple:
    """(count, known) commits between two shas that touch anything but .coord.

    THE ONE COUNTING RULE. The lag warning, `--if-stale` and the hooks all
    reach it through lag(); there is no second copy, because a second copy
    is exactly how this bug happened - is_dirty() learned to exclude .coord
    and the behind-count shipped without the same exclusion.

    `known` is False when git cannot walk from `was` to `sha` at all: a
    rebase, a force-push or a shallow clone can leave the recorded sha
    unreachable. That is NOT the same as "no code changed", and collapsing
    the two would let a rewritten history read as current. Unknown reports.
    """
    if was == sha:
        return 0, True
    out, ok = _git(repo, "rev-list", "--count", f"{was}..{sha}",
                   "--", ".", ":(exclude).coord")
    if ok and out.isdigit():
        return int(out), True
    return 0, False


def read_manifest(coord) -> dict:
    try:
        d = json.loads((Path(coord) / "manifest.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def record(coord, repo) -> None:
    """Stamp the manifest with the sha the map was just built from.

    Called at the END of an index. Additive: existing keys are preserved,
    so a manifest written by any earlier version keeps whatever it had.
    """
    coord = Path(coord)
    man = read_manifest(coord)
    sha = head_sha(repo)
    if sha:
        man[SHA_KEY] = sha
        man[DIRTY_KEY] = is_dirty(repo)
        # The staged tree, excluding .coord. Recorded alongside the sha
        # rather than instead of it: the sha still answers "which commit
        # was this built from", which lineage and dig want, while the tree
        # answers "does this map still describe the code", which is the
        # only question freshness should ask.
        tree = code_tree(repo)
        if tree:
            man[TREE_KEY] = tree
    try:
        (coord / "manifest.json").write_text(json.dumps(man, indent=1) + "\n")
    except OSError:
        pass                     # a map that cannot record is still a map


def lag(repo, coord) -> dict:
    """How far the map trails the tree. {} means nothing to report.

    Returns {} - not a warning - in every case where an honest answer is
    unavailable: no git, no manifest, no recorded sha. Silence is correct
    there; a warning that fires when it cannot know is noise, and noise
    is how a real warning gets ignored.

    THE INCIDENT THAT PROVED IT. This shipped gating on `was == sha`, so
    committing the map - the workflow memway explicitly recommends - left
    every repo permanently one commit behind itself and warning about it
    forever. A warning that always fires is not a warning; it had become
    exactly the noise the paragraph above was written to avoid.

    is_dirty() had already learned to exclude .coord, for the same reason,
    weeks earlier. The behind-count shipped without the exclusion because
    it was a SECOND COPY of one rule and only the first copy got fixed.
    The cure is not remembering harder next time; it is
    code_commits_between being the only place that counts, so there is
    nowhere for a second answer to live.
    """
    sha = head_sha(repo)
    if not sha:
        return {}                                # not a git repo
    man = read_manifest(coord)
    was = man.get(SHA_KEY, "")
    if not was:
        return {}                                # map predates the stamp
    dirty = is_dirty(repo)

    # CONTENT FIRST. A recorded tree hash answers the real question
    # directly: does this map describe the code that is committed? If yes,
    # nothing else matters - not how many commits happened, not whether a
    # rebase renumbered them, not which worktree this is. The commit-
    # counting path below stays for maps written before the tree was
    # recorded; it must never slander them (the 0.53.x pattern: absent
    # means old, and old means fall back, not fail).
    tree_was = man.get(TREE_KEY, "")
    if tree_was and not dirty:
        if tree_was == head_tree(repo):
            return {}
    behind, known = code_commits_between(repo, was, sha)
    # COMMITTING THE MAP MOVES HEAD. memway tells you to commit .coord, so
    # `was != sha` the instant you do - while the map still describes the
    # code exactly. Gating on sha equality made the warning fire forever
    # after any map commit, on the very workflow this project recommends.
    # What matters is whether any CODE moved, not whether HEAD did.
    if known and behind == 0 and not dirty:
        return {}                                # the map still fits the code
    return {"indexed_at": was, "head": sha, "behind": behind,
            "dirty": dirty, "known": known,
            "message": message(was, sha, behind, dirty, known)}


def message(was: str, sha: str, behind: int, dirty: bool,
            known: bool = True) -> str:
    """The one line a read tool prints. Says what, and what to do."""
    if not known:
        return (f"map indexed at {was[:7]}, which this repo cannot reach from "
                f"HEAD {sha[:7]} (rebased, force-pushed or shallow) "
                f"- run memway index")
    if behind == 0 and dirty:
        return (f"map indexed at {was[:7]}, working tree has uncommitted "
                f"changes - run memway index")
    n = (f" ({behind} commit{'s' if behind != 1 else ''} ahead)"
         if behind else "")
    tail = " and uncommitted changes" if dirty else ""
    return (f"map indexed at {was[:7]}, HEAD is {sha[:7]}{n}{tail} "
            f"- run memway index")
