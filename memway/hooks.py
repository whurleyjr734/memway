"""Git hooks that keep the map synced, installed only when asked.

OPT-IN, ALWAYS. `memway setup` advertises this and does not run it. A
tool that writes into .git/hooks uninvited has taken something that was
not offered, and no amount of usefulness buys that back.

A HOOK MUST NEVER BREAK A COMMIT. Every hook here ends in `exit 0`, and
the memway line is guarded so a failure cannot propagate. Someone whose
commit was blocked by an indexing tool will remove the tool, correctly.
Errors go to .coord/log/hooks.log and the commit proceeds.

MARKER DISCIPLINE, borrowed from the rules files. The memway line lives
between two markers, appended to whatever is already there. `uninstall`
removes exactly that block and nothing else. A hook that exists and does
not carry the markers is left alone and reported, because rewriting
somebody's pre-existing hook is the same trespass as rewriting their
CLAUDE.md.

POSIX sh, no bashisms: these run under whatever /bin/sh is, which on
Debian is dash and will not forgive [[ ]] or arrays.
"""

import os
import stat
from pathlib import Path

# post-commit  : the common case, a commit moved HEAD
# post-checkout: branch switches and `git checkout <sha>`, which a
#                post-commit hook never sees
# post-merge   : pull and merge, same reason
# post-commit  : the common case, a commit moved HEAD
# post-checkout: branch switches and `git checkout <sha>`
# post-merge   : pull and merge
# pre-commit   : the ONLY one that fires BEFORE the work is sealed, and
#                the only one that can tell you what you are about to
#                commit having invalidated. It reports and never blocks.
HOOKS = ("post-commit", "post-checkout", "post-merge", "pre-commit")
PRE_COMMIT = "pre-commit"

BEGIN = "# >>> memway >>>"
END = "# <<< memway <<<"

# Short enough to read in one glance, which is the point: a hook nobody
# can read is a hook nobody trusts. `|| true` is belt and braces on top
# of --if-stale's own exit-0 contract.
#
# THE PATH IS PINNED AT INSTALL TIME, absolute. A bare `memway` needs the
# right PATH, and a git hook inherits whatever environment invoked git -
# a GUI client, an IDE, a shell without the venv activated. Measured: the
# hook printed "memway: command not found" from a shell that had not
# activated the venv, and `|| true` swallowed it, so the map silently
# stopped syncing while every surface reported hooks installed.
#
# If the venv is later moved or deleted the pinned path stops resolving
# and the hook goes quiet again - which is why the lag warning on every
# read tool, not this hook, is the actual guarantee.
def block_for(exe: str) -> str:
    return f"""{BEGIN}
# keeps the map in step with the tree. remove with: memway hooks uninstall
# path pinned at install time; if it stops resolving (venv moved or gone)
# the hook goes quiet, and the map-lag warning on reads is the backstop.
{exe} index . --if-stale --quiet || true
{END}"""


def pre_commit_block_for(exe: str) -> str:
    """Report what you invalidated, then put the map INSIDE the commit.

    BOTH KINDS, since 0.55.4: the knowledge this change staled, and the
    comments it rotted. The grep names both sections, because one that
    named only STALED KNOWLEDGE would send the other into a pipe nobody
    reads - and a report the hook cannot see is a report that does not
    exist. This docstring said "what you staled" and was caught by the
    very feature it describes, on the release that added it.

    EXITS 0 ALWAYS, on every path: each command carries `|| true` and the
    block adds no exit of its own, so a fresh hook's trailing `exit 0` or
    a foreign hook's own control flow decides the status. A hook that
    blocks a commit gets removed, correctly - and "your map is stale" has
    less right to block than anything.

    ORDER IS THE DESIGN, and it is not the obvious one. The staleness
    report runs FIRST, against the pre-index state: that is the only
    moment it can see what your change invalidated. Index first and the
    report goes quiet, because the index would already agree with the
    tree it was about to describe - the feature would still be installed
    and would silently stop telling you anything.

    Then index, then `git add .coord`, so the map lands in the same commit
    as the code it describes. Before this the map arrived one commit late
    and every change cost two commits: the post-commit hook re-indexed,
    left .coord dirty, and the next `git checkout` refused to switch
    branches until you committed the stamp. That happened twice in one
    session on 2026-08-16, mid-merge, to the person who wrote the hook.

    `git add .coord` is scoped to .coord and nothing else. It must never
    become `git add -A`: a hook that stages the user's unrelated work has
    taken a decision that was not offered to it.
    """
    return f"""{BEGIN}
# 1. what did this change invalidate? asked BEFORE the index moves.
#    BOTH sections: knowledge the change staled, and comments it rotted.
#    The grep used to name STALED KNOWLEDGE alone, so the rot section
#    added in 0.55.4 would have printed into a pipe nobody read - a
#    report the hook could not see is a report that does not exist.
{exe} verify-change . 2>/dev/null | grep -E -A 99 "STALED KNOWLEDGE|COMMENT ROT" || true
# 2. refresh the map and put it IN this commit, so code and map arrive
#    together. remove with: memway hooks uninstall
{exe} index . --if-stale --quiet || true
git add .coord 2>/dev/null || true
{END}"""


def memway_exe() -> str:
    """The invocation to pin into a hook: absolute, and THIS memway.

    Not resolve(). A venv's bin/python is usually a SYMLINK to the base
    interpreter, so resolving it walks out of the venv entirely - measured:
    installing from .venv/bin/python pinned the framework Python's memway,
    a different install at a different version, and the hook then ran the
    wrong tool with a straight face.

    Falls back to `<python> -m memway.cli` when no console script exists,
    which is the case in a plain source checkout. Uglier, always true.
    """
    import shutil
    import sys
    argv0 = Path(sys.argv[0])
    if argv0.name in ("memway", "memway.exe") and argv0.exists():
        return f'"{argv0.absolute()}"'
    beside = Path(sys.executable).absolute().with_name("memway")
    if beside.exists():
        return f'"{beside}"'
    found = shutil.which("memway")
    if found:
        return f'"{Path(found).absolute()}"'
    return f'"{Path(sys.executable).absolute()}" -m memway.cli'


BLOCK = block_for('"memway"')          # marker detection only; never written

SHEBANG = "#!/bin/sh\n"


def commands_for(name: str, exe: str = "memway") -> list:
    """The commands a given hook actually runs, read out of its own body.

    THE ONE SOURCE for anything that describes the hooks. `hooks install`
    printed "each runs: memway index . --if-stale --quiet" as a constant,
    which stopped being true in the same release that made pre-commit
    report, index and stage - the banner described behaviour the code no
    longer had, on the very command that installs it.

    That is the third time this shape has bitten: a browser tab naming
    somebody else's project, a wordmark drifted from the site it belongs
    to, and now this. Constants that describe behaviour are not checked by
    tests that check behaviour, so the fix is always the same - derive it,
    and pin the derivation.
    """
    block = (pre_commit_block_for(exe) if name == PRE_COMMIT
             else block_for(exe))
    return [l.strip() for l in block.splitlines()
            if l.strip() and not l.lstrip().startswith("#")
            and l.strip() not in (BEGIN, END)]


def describe() -> list:
    """Human-readable lines for the install banner, derived not restated."""
    out = []
    for name in HOOKS:
        cmds = commands_for(name)
        shown = [c.split(" 2>")[0].split(" || ")[0].replace("memway ", "", 1)
                 for c in cmds]
        out.append(f"  {name}: " + "; ".join(shown))
    return out


def hooks_dir(repo) -> Path:
    """.git/hooks, resolving the .git FILE that worktrees use."""
    g = Path(repo) / ".git"
    if g.is_file():                      # linked worktree: .git is a file
        try:
            line = g.read_text().strip()
            if line.startswith("gitdir:"):
                g = Path(line.split(":", 1)[1].strip())
                if not g.is_absolute():
                    g = (Path(repo) / g).resolve()
        except OSError:
            pass
    return g / "hooks"


def plan(path: Path, exe: str = "") -> tuple:
    """(content_or_None, message) for one hook file. Never clobbers."""
    name = path.name
    exe = exe or memway_exe()
    BLOCK = (pre_commit_block_for(exe) if name == PRE_COMMIT
             else block_for(exe))
    if not path.exists():
        return SHEBANG + "\n" + BLOCK + "\n\nexit 0\n", f"wrote {name}"
    cur = path.read_text()
    if BEGIN in cur and END in cur:
        # UPGRADE IN PLACE, the marker-block pattern the rules files use.
        # This used to return "leaving it" unconditionally, which meant an
        # existing install NEVER received a changed hook body: 0.55.0's
        # pre-commit stages the map, and every repo that had installed
        # hooks before would have upgraded memway and silently kept the
        # old behaviour. The markers exist precisely so the managed region
        # can be replaced without touching anything around it.
        i, j = cur.index(BEGIN), cur.index(END) + len(END)
        if cur[i:j] == BLOCK:
            return None, f"{name} already current - leaving it"
        return (cur[:i] + BLOCK + cur[j:],
                f"upgraded {name} (kept everything outside the markers)")
    if not cur.startswith("#!"):
        return None, (f"{name} exists and has no shebang, so appending is "
                      f"not safe - REFUSING (add {BEGIN} / {END} around a "
                      f"memway line yourself to opt in)")
    # AN APPEND AFTER `exit` IS DEAD CODE. Git's own sample hooks end in
    # `exit 0`, and appending past one installs cleanly, reports success,
    # and never runs - the worst failure available here, because nothing
    # says so. Measured on a fixture: the block landed below `exit 0` and
    # the map silently stopped tracking.
    #
    # HEURISTIC, AND ITS LIMIT: this sees `exit` only where it STARTS a
    # line. `if [ -n "$SKIP" ]; then exit 0; fi` is invisible to it, and
    # such a hook gets the block inserted above its final exit - which is
    # the right outcome anyway, since an early conditional exit is the
    # author asking to skip everything after it.
    #
    # So: find top-level `exit` lines (unindented, outside comments). None
    # means a plain append is safe. Exactly one, as the final statement,
    # means insert above it. Anything else is control flow this has no
    # business guessing at, and is refused.
    lines = cur.splitlines()
    exits = [i for i, l in enumerate(lines)
             if l.strip().startswith("exit") and l == l.lstrip()]
    body = cur if cur.endswith("\n") else cur + "\n"
    if not exits:
        # Safe to append last: git ignores the exit status of post-commit,
        # post-checkout and post-merge, so `|| true` becoming the script's
        # final status changes nothing git acts on. No `exit 0` is added -
        # it would survive uninstall as residue in somebody else's file.
        return body + "\n" + BLOCK + "\n", f"appended to {name}"
    tail = [i for i, l in enumerate(lines)
            if l.strip() and not l.strip().startswith("#")]
    if len(exits) == 1 and tail and exits[0] == tail[-1]:
        i = exits[0]
        out = lines[:i] + BLOCK.splitlines() + [""] + lines[i:]
        return "\n".join(out) + "\n", f"inserted into {name} above its exit"
    return None, (f"{name} exits partway through, so a memway line cannot be "
                  f"placed safely without guessing at its control flow - "
                  f"REFUSING (put {BEGIN} / {END} around a memway line "
                  f"wherever it belongs and rerun)")


def strip_block(text: str) -> str:
    """Remove exactly the marked block, leave everything else byte-identical."""
    if BEGIN not in text or END not in text:
        return text
    i = text.index(BEGIN)
    j = text.index(END) + len(END)
    out = text[:i] + text[j:]
    # tidy the blank lines the block was sitting in, nothing more. The
    # trailing collapse matters: without it an uninstall left one extra
    # newline in somebody else's file, so the round-trip was very nearly
    # byte-identical, which is not the same thing.
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    while out.endswith("\n\n"):
        out = out[:-1]
    return out


def install(repo) -> list:
    d = hooks_dir(repo)
    if not d.parent.exists():
        return [f"no .git at {repo} - nothing to install into"]
    d.mkdir(parents=True, exist_ok=True)
    exe = memway_exe()
    out = []
    for name in HOOKS:
        p = d / name
        content, msg = plan(p, exe)
        if content is not None:
            p.write_text(content)
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        out.append(msg)
    return out


def uninstall(repo) -> list:
    d = hooks_dir(repo)
    out = []
    for name in HOOKS:
        p = d / name
        if not p.exists():
            out.append(f"{name}: not present")
            continue
        cur = p.read_text()
        if BEGIN not in cur:
            out.append(f"{name}: no memway block - leaving it")
            continue
        rest = strip_block(cur)
        # If nothing but our own scaffolding is left, remove the file we
        # created rather than leaving an empty hook behind.
        meat = [l for l in rest.splitlines()
                if l.strip() and not l.startswith("#!") and l.strip() != "exit 0"]
        if not meat:
            p.unlink()
            out.append(f"{name}: removed (was ours alone)")
        else:
            p.write_text(rest)
            out.append(f"{name}: memway block removed, your hook kept")
    return out
