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
HOOKS = ("post-commit", "post-checkout", "post-merge")

BEGIN = "# >>> memway >>>"
END = "# <<< memway <<<"

# Short enough to read in one glance, which is the point: a hook nobody
# can read is a hook nobody trusts. `|| true` is belt and braces on top
# of --if-stale's own exit-0 contract.
BLOCK = f"""{BEGIN}
# keeps the map in step with the tree. remove with: memway hooks uninstall
memway index . --if-stale --quiet || true
{END}"""

SHEBANG = "#!/bin/sh\n"


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


def plan(path: Path) -> tuple:
    """(content_or_None, message) for one hook file. Never clobbers."""
    name = path.name
    if not path.exists():
        return SHEBANG + "\n" + BLOCK + "\n\nexit 0\n", f"wrote {name}"
    cur = path.read_text()
    if BEGIN in cur and END in cur:
        return None, f"{name} already has the memway block - leaving it"
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
    out = []
    for name in HOOKS:
        p = d / name
        content, msg = plan(p)
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
