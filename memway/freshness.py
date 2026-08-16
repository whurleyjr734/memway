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

import json
import subprocess
from pathlib import Path

SHA_KEY = "indexed_at_sha"
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
    """
    sha = head_sha(repo)
    if not sha:
        return {}                                # not a git repo
    man = read_manifest(coord)
    was = man.get(SHA_KEY, "")
    if not was:
        return {}                                # map predates the stamp
    dirty = is_dirty(repo)
    if was == sha and not dirty:
        return {}                                # current
    behind = 0
    if was != sha:
        out, ok = _git(repo, "rev-list", "--count", f"{was}..{sha}")
        if ok and out.isdigit():
            behind = int(out)
    return {"indexed_at": was, "head": sha, "behind": behind,
            "dirty": dirty, "message": message(was, sha, behind, dirty)}


def message(was: str, sha: str, behind: int, dirty: bool) -> str:
    """The one line a read tool prints. Says what, and what to do."""
    if was == sha:
        return (f"map indexed at {was[:7]}, working tree has uncommitted "
                f"changes - run memway index")
    n = (f" ({behind} commit{'s' if behind != 1 else ''} ahead)"
         if behind else "")
    tail = " and uncommitted changes" if dirty else ""
    return (f"map indexed at {was[:7]}, HEAD is {sha[:7]}{n}{tail} "
            f"- run memway index")
