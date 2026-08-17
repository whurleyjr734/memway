"""The two frictions that get a tool uninstalled.

Neither is a correctness bug. Both are the kind of daily grit that makes
someone quietly stop using something, which is worse than a crash because
nobody files it.

ONE: the map arrived one commit late. The post-commit hook re-indexed
AFTER the commit, leaving .coord dirty, so every change cost two commits -
and `git checkout` refuses to switch branches with a dirty tree. That
happened twice in one session on 2026-08-16, mid-merge, to the person who
wrote the hook.

TWO: an MCP server keeps the code it started with. Upgrading memway
underneath a live agent leaves it on the old build silently - and once,
not silently at all: 0.54.3 added a field to RawEdge and the old server
raised `unexpected keyword argument` on every call.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import freshness, mcp

SRC = 'def widget(x):\n    """D."""\n    return x + 1\n'
EDIT = 'def widget(x):\n    """D."""\n    return x + 99\n'


def _git(r, *a):
    return subprocess.run(["git", "-C", str(r), *a],
                          capture_output=True, text=True)


def _commit(r, msg):
    return _git(r, "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-m", msg, "--no-gpg-sign")


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "memway.cli", *args],
                          capture_output=True, text=True, cwd=str(HERE))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _commit(r, "one")
    assert _cli("init", str(r)).returncode == 0
    assert _cli("hooks", "install", str(r)).returncode == 0
    _git(r, "add", "-A")
    _commit(r, "commit the map")
    assert not _git(r, "status", "--porcelain").stdout.strip(), "fixture starts dirty"
    return r


# ------------------------------------------- ITEM 1: the map rides along

def test_a_change_is_ONE_commit_and_the_tree_is_clean_after(repo):
    """THE regression. Before: commit, hook re-indexes, .coord dirty,
    commit again - and a branch switch in between simply refuses."""
    before = int(_git(repo, "rev-list", "--count", "HEAD").stdout)
    (repo / "m.py").write_text(EDIT)
    _git(repo, "add", "m.py")
    r = _commit(repo, "change widget")
    assert r.returncode == 0, r.stderr
    after = int(_git(repo, "rev-list", "--count", "HEAD").stdout)

    assert after - before == 1, f"{after - before} commits for one change"
    assert not _git(repo, "status", "--porcelain").stdout.strip(), \
        "tree dirty after the commit - the two-commit dance is back"
    staged = _git(repo, "show", "--stat", "--name-only", "HEAD").stdout
    assert ".coord/" in staged, "the map did not ride inside the commit"


def test_freshness_reads_CURRENT_after_that_commit(repo):
    """The landmine. The map is indexed DURING pre-commit, so its recorded
    sha is the PREVIOUS HEAD - a commit-sha comparison reports behind: 1
    on a map that describes the code exactly. This test must fail against
    a version that compares shas.
    """
    (repo / "m.py").write_text(EDIT)
    _git(repo, "add", "m.py")
    _commit(repo, "change widget")

    man = json.loads((repo / ".coord" / "manifest.json").read_text())
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert man["indexed_at_sha"] != head, (
        "fixture no longer exercises the trap: the recorded sha must lag "
        "HEAD, or comparing shas would pass by accident")
    assert man["indexed_at_tree"] == freshness.head_tree(repo)
    assert freshness.lag(repo, repo / ".coord") == {}, \
        "a map describing exactly this code was reported as behind"


def test_the_tree_hash_excludes_coord(repo):
    """It has to. The hash is recorded INTO .coord/manifest.json, so a
    hash covering .coord would change the tree it describes and could
    never match on the next read. `git write-tree` - the obvious tool -
    covers .coord, which is why it is not used."""
    before = freshness.code_tree(repo)
    (repo / ".coord" / "scratch.txt").write_text("noise")
    _git(repo, "add", ".coord")
    assert freshness.code_tree(repo) == before, \
        "the tree hash moved when only .coord changed"
    (repo / "m.py").write_text(EDIT)
    _git(repo, "add", "m.py")
    assert freshness.code_tree(repo) != before, \
        "the tree hash ignored a real code change"


def test_a_map_with_no_tree_hash_is_not_slandered(repo):
    """Maps written before 0.55 have no tree recorded. Absent means old,
    and old means fall back to the commit path - never fail."""
    p = repo / ".coord" / "manifest.json"
    man = json.loads(p.read_text())
    del man[freshness.TREE_KEY]
    p.write_text(json.dumps(man))
    freshness.lag(repo, repo / ".coord")          # must not raise
    assert freshness.code_tree(repo)              # still computable


def test_the_post_commit_hook_becomes_a_no_op(repo):
    """It stays installed for bisect, rebase and hand-edited trees, and
    does nothing when pre-commit already did the work - which the tree
    comparison makes free."""
    (repo / "m.py").write_text(EDIT)
    _git(repo, "add", "m.py")
    _commit(repo, "change widget")
    r = subprocess.run([sys.executable, "-m", "memway.cli", "index",
                        str(repo), "--if-stale"],
                       capture_output=True, text=True, cwd=str(HERE))
    assert "current" in r.stdout.lower(), r.stdout
    assert not _git(repo, "status", "--porcelain").stdout.strip()


@pytest.mark.parametrize("breakage", ["no-memway", "index-fails"])
def test_the_hook_never_blocks_a_commit(repo, breakage):
    """Every failure path exits 0. A hook that blocks a commit gets
    removed, and 'your map is stale' has less right to block than
    anything."""
    hook = repo / ".git" / "hooks" / "pre-commit"
    body = hook.read_text()
    if breakage == "no-memway":
        import re
        body = re.sub(r'"[^"]*memway"', '"/nonexistent/memway"', body)
    else:
        body = body.replace("index . --if-stale --quiet",
                            "index /nonexistent/repo --if-stale")
    hook.write_text(body)

    (repo / "m.py").write_text(EDIT)
    _git(repo, "add", "m.py")
    r = _commit(repo, "commit with a broken hook")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert _git(repo, "log", "--oneline", "-1").stdout.strip()


def test_the_hook_stages_only_coord(repo):
    """`git add .coord`, never `git add -A`. A hook that stages the user's
    unrelated work has taken a decision nobody offered it."""
    hook = (repo / ".git" / "hooks" / "pre-commit").read_text()
    assert "git add .coord" in hook, hook
    assert "git add -A" not in hook and "git add ." not in hook.replace(
        "git add .coord", ""), hook

    (repo / "m.py").write_text(EDIT)
    (repo / "unrelated.txt").write_text("do not stage me")
    _git(repo, "add", "m.py")
    _commit(repo, "change widget")
    assert "unrelated.txt" in _git(repo, "status", "--porcelain").stdout, \
        "the hook staged a file the user had not staged"


def test_upgrading_an_existing_install_keeps_foreign_hooks(tmp_path):
    """Migration through the marker machinery: somebody else's pre-commit
    survives, ours is added below it."""
    r = tmp_path / "existing"
    r.mkdir()
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    hooks = r / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    theirs = hooks / "pre-commit"
    theirs.write_text("#!/bin/sh\necho 'their linter'\n")
    theirs.chmod(0o755)
    _cli("init", str(r))

    assert _cli("hooks", "install", str(r)).returncode == 0
    body = theirs.read_text()
    assert "their linter" in body, "we clobbered their hook"
    assert "git add .coord" in body, "ours was not added"


# ------------------------------------ ITEM 2: the version handshake

@pytest.fixture(autouse=True)
def _reset_announced():
    mcp._ANNOUNCED = False
    yield
    mcp._ANNOUNCED = False


def test_matching_versions_say_nothing(monkeypatch):
    """Pinned against what the process actually runs, not against the
    installed dist. In an editable checkout the two genuinely differ the
    moment the version is bumped and before anything is reinstalled -
    which is real drift, correctly reported, and would make a naive
    version of this test fail during every release."""
    monkeypatch.setattr("importlib.metadata.version",
                        lambda n: mcp._SERVER_VERSION)
    assert mcp.version_drift() == ""


def test_a_drifted_server_announces_itself_exactly_once(monkeypatch):
    monkeypatch.setattr("importlib.metadata.version", lambda n: "99.0.0")
    first = mcp.version_drift()
    assert "restart your MCP server" in first, first
    assert mcp._SERVER_VERSION in first and "99.0.0" in first, first
    assert mcp.version_drift() == "", \
        "repeated every call - the condition cannot resolve while the " \
        "process lives, so repeating it trains the reader to scroll past"


def test_a_drifted_server_still_ANSWERS(monkeypatch, tmp_path):
    """Never refuse. A stale server that answers beats a dead one
    mid-session: the agent finishes its task and restarts after."""
    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    _cli("init", str(r))
    monkeypatch.setattr("importlib.metadata.version", lambda n: "99.0.0")

    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "memway_summary", "arguments": {}}}
    resp = mcp.handle(msg, str(r))
    assert resp and "result" in resp, resp
    assert not resp["result"].get("isError"), resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "server_version_drift" in payload, list(payload)
    assert "restart your MCP server" in payload["server_version_drift"]
    # the real answer is still there
    assert any(k != "server_version_drift" for k in payload), payload


def test_an_unknowable_version_is_not_treated_as_drift(monkeypatch):
    """cannot tell != stale. If metadata is unavailable, say nothing."""
    def boom(_):
        raise RuntimeError("no metadata")
    monkeypatch.setattr("importlib.metadata.version", boom)
    assert mcp.version_drift() == ""


def test_it_cannot_block_a_commit_inside_someone_elses_hook(tmp_path):
    """Where the guards actually earn their place - and the fixture that
    took three attempts to make honest.

    A FRESH hook file gets a trailing `exit 0` from plan(), which masks
    everything before it. And in sh the script's status is the LAST
    command's status, so guards on earlier lines are defensive, not
    load-bearing - removing `|| true` from the index line changed no
    outcome at all.

    The failure that can really block a commit: memway is missing, so the
    index never runs, so .coord is never created, so `git add .coord`
    FAILS - as the final command, inside a foreign hook that adds no
    `exit 0` of its own. Without the guard on that line, installing
    memway into a repo that already has a linter would block every commit
    from a machine where memway had been uninstalled or moved.
    """
    r = tmp_path / "foreign"
    r.mkdir()
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _commit(r, "one")
    hooks = r / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    theirs = hooks / "pre-commit"
    theirs.write_text("#!/bin/sh\necho 'their linter ran'\n")
    theirs.chmod(0o755)

    # NO memway init: there is no .coord, so `git add .coord` has nothing
    # to add and fails. That is the state a machine is in when memway was
    # uninstalled but its hooks were left behind.
    assert _cli("hooks", "install", str(r)).returncode == 0
    body = theirs.read_text()
    assert "their linter ran" in body, "we clobbered their hook"
    assert "exit 0" not in body, (
        "plan() added an exit 0 into somebody else's file - residue that "
        "survives uninstall, and it would also mask this test")
    assert not (r / ".coord").exists(), "fixture created a map it should not have"

    import re as _re
    theirs.write_text(_re.sub(r'"[^"]*memway"', '"/nonexistent/memway"', body))

    (r / "m.py").write_text(EDIT)
    _git(r, "add", "m.py")
    res = _commit(r, "commit with memway gone and no map")
    assert res.returncode == 0, (
        f"a clean commit was BLOCKED: exit {res.returncode}\n"
        f"{res.stdout}\n{res.stderr}")
    assert _git(r, "log", "--oneline", "-1").stdout.strip()


def test_an_existing_install_is_UPGRADED_not_left_alone(tmp_path):
    """The migration that nearly didn't happen.

    `plan()` returned "already has the memway block - leaving it" for any
    file carrying the markers, so an existing install could never receive
    a changed hook body. 0.55.0 changes what pre-commit DOES, and every
    repo that had run `hooks install` before would have upgraded memway
    and silently kept the old behaviour - the feature shipped, installed,
    and inert.

    Caught by running `hooks install` on memway's own repo before
    committing, rather than on a fresh fixture where the question cannot
    arise.
    """
    r = tmp_path / "old"
    r.mkdir()
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _commit(r, "one")
    _cli("init", str(r))
    assert _cli("hooks", "install", str(r)).returncode == 0

    hook = r / ".git" / "hooks" / "pre-commit"
    body = hook.read_text()
    # forge a PREVIOUS version's block: same markers, older content
    from memway.hooks import BEGIN, END
    i, j = body.index(BEGIN), body.index(END) + len(END)
    old_block = f"{BEGIN}\n# an older memway block\necho old\n{END}"
    hook.write_text(body[:i] + old_block + body[j:] + "\n# my own line\n")

    out = _cli("hooks", "install", str(r))
    assert out.returncode == 0, out.stderr
    assert "upgraded" in out.stdout, out.stdout
    after = hook.read_text()
    assert "echo old" not in after, "the stale block survived the upgrade"
    assert "git add .coord" in after, "the new block did not arrive"
    assert "# my own line" in after, "content outside the markers was lost"

    # and it must be idempotent - a second run changes nothing
    again = _cli("hooks", "install", str(r))
    assert "already current" in again.stdout, again.stdout
    assert hook.read_text() == after, "a no-op run rewrote the file"


# PARKED FOR THE 0.55.x COLLABORATION BOARD, deliberately not built here:
# self-consistency across a CONFLICT-RESOLVED merge. Verified today on a
# clean merge only - git composes the merge tree from both parents and
# neither parent's .coord describes the result, so the map that lands is
# whichever side won the merge, which may describe neither. A clean merge
# happened to come out self-consistent because one parent's map already
# fitted the merged code; a conflicted one has no such guarantee. Testing
# it needs a fixture that resolves a real conflict, and the fix - if it
# needs one - belongs with the rest of the multi-author story.


# ------------------------------ the banner describes what is installed

def test_the_install_banner_names_every_command_the_hooks_run(tmp_path):
    """Derived, not restated - the wordmark pattern applied to a banner.

    `hooks install` printed "each runs: memway index . --if-stale --quiet"
    as a constant. That stopped being true in the same release that made
    pre-commit report, index and stage: the command that installs the
    hooks described behaviour they no longer had.

    Third instance of this shape - a tab naming somebody else's project, a
    wordmark drifted from its site, and this. Constants that describe
    behaviour are invisible to tests that check behaviour, so the banner
    is compared against the bodies of the hooks ACTUALLY WRITTEN TO DISK,
    not against a literal.
    """
    from memway import hooks as h
    r = tmp_path / "banner"
    r.mkdir()
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    _cli("init", str(r))
    out = _cli("hooks", "install", str(r))
    assert out.returncode == 0, out.stderr
    banner = out.stdout

    lines = {l.strip().split(":")[0]: l for l in banner.splitlines()
             if l.strip().split(":")[0] in h.HOOKS}
    for name in h.HOOKS:
        assert name in lines, f"{name} has no banner line:\n{banner}"
        row = lines[name]
        body = (r / ".git" / "hooks" / name).read_text()
        installed = [l.strip() for l in
                     body[body.index(h.BEGIN):body.index(h.END)].splitlines()
                     if l.strip() and not l.lstrip().startswith("#")]
        assert installed, f"{name} has no commands"
        for cmd in installed:
            # normalised HERE, independently of hooks.describe(), or this
            # test would be comparing the derivation against itself.
            # Checked against THAT HOOK'S line, not the whole banner - the
            # first version matched `git` against the trailing sentence
            # "a failure never blocks the git operation" and passed a
            # sabotage that dropped two commands.
            core = cmd.split(" 2>")[0].split(" || ")[0].strip()
            if core.startswith('"'):
                core = core.split('"', 2)[2].strip()      # drop the exe path
            key = " ".join(core.split()[:2])
            assert key in row, (
                f"{name} runs {key!r} and its banner line does not say so:\n"
                f"  line: {row}\n  full banner:\n{banner}")


def test_the_banner_is_not_a_hardcoded_sentence():
    """Guard the guard: a banner built from a literal would satisfy the
    test above today and drift again tomorrow."""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "memway" / "cli.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_hooks")
    dump = ast.dump(fn)
    assert "describe" in dump, "the banner no longer derives from hooks.describe()"
    for literal in ("--if-stale --quiet", "each runs"):
        assert literal not in dump, \
            f"cmd_hooks hardcodes {literal!r} instead of deriving it"


# ------------------------------------ the migration message derives too

def test_the_migration_message_names_the_versions_it_migrates(tmp_path):
    """EXECUTED against a real v2 map, because the string is not behaviour.

    This printed "(v1 -> v2)" as a constant while SKETCH_VERSION was 3:
    a sentence announcing a migration nobody was performing, sitting two
    lines from the number it misquoted. Every test on this path asserts
    what the index DOES, and the index did the right thing the whole time.
    """
    import json
    import subprocess
    import sys
    from pathlib import Path

    HERE = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(HERE))
    from memway.indexer import SKETCH_VERSION

    r = tmp_path / "proj"
    r.mkdir()
    (r / "m.py").write_text("def alpha(x):\n    return x + 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)

    def cli(*a):
        return subprocess.run([sys.executable, "-m", "memway.cli", *a],
                              capture_output=True, text=True, cwd=str(HERE))

    assert cli("init", str(r)).returncode == 0

    # age the map to generation 2 - the migration is 2 -> SKETCH_VERSION
    man_p = r / ".coord" / "manifest.json"
    man = json.loads(man_p.read_text())
    man["sketch_version"] = 2
    man_p.write_text(json.dumps(man, indent=1) + "\n")

    (r / "m.py").write_text("def alpha(x):\n    return x + 2\n")
    out = cli("index", str(r)).stdout
    assert "sketch generation changed" in out, f"no migration announced:\n{out}"
    assert f"(v2 -> v{SKETCH_VERSION})" in out, (
        f"message does not name the real versions (SKETCH_VERSION="
        f"{SKETCH_VERSION}):\n{out}")


def test_the_migration_message_is_not_a_hardcoded_sentence():
    """Guard the guard, exactly as the banner has one."""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "memway" / "cli.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_index")
    dump = ast.dump(fn)
    assert "stored_sketch_version" in dump, \
        "the migration message no longer derives the stored version"
    assert "SKETCH_VERSION" in dump, \
        "the migration message no longer derives the current version"
    for literal in ("v1 -> v2", "v2 -> v3"):
        assert literal not in dump, \
            f"cmd_index hardcodes {literal!r} instead of deriving it"


def test_a_CRASHING_drifted_server_still_says_restart_me(monkeypatch,
                                                         tmp_path):
    """The notice rides the session, not the success path.

    The incident this handshake was built for is an old server dying on a
    map the new build re-indexed - it raised
    `RawEdge.__init__() got an unexpected keyword argument 'via_attr'`
    on EVERY call. version_drift() was asked after the tool call and
    inside the same try, so the crash returned a bare {"error": ...} and
    the one sentence that explains it - restart your MCP server - was
    never computed. Reproduced on this repo's own server, 2026-08-16,
    where it had been silently stale for hours.

    Executed against a tool that raises, because that IS the condition.
    """
    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    _cli("init", str(r))
    monkeypatch.setattr("importlib.metadata.version", lambda n: "99.0.0")
    monkeypatch.setattr(mcp, "_ANNOUNCED", False)

    def boom(repo, args):
        raise TypeError("RawEdge.__init__() got an unexpected keyword "
                        "argument 'via_attr'")

    monkeypatch.setitem(mcp._BY_NAME["memway_summary"], "fn", boom)

    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "memway_summary", "arguments": {}}}
    resp = mcp.handle(msg, str(r))
    assert resp and resp["result"].get("isError"), resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "error" in payload, payload
    assert "server_version_drift" in payload, (
        f"the crash carried no restart notice - exactly the case the "
        f"handshake exists for. keys: {list(payload)}")
    assert "restart" in payload["server_version_drift"].lower()


def test_hooks_install_upgrades_an_OLD_block_in_place(tmp_path):
    """0.55.4's rot section only reaches a human if the hook's grep is
    widened - and every repo that ran `hooks install` before this release
    carries the narrow one.

    This is the marker machinery from 0.55.0 being cashed in: the block
    between the markers is REPLACED, everything outside it is left alone
    byte for byte. Without that, the release would ship a report the hook
    could not see, in every repo that already had memway installed.
    """
    from memway.hooks import BEGIN, END

    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text(SRC)
    _git(r, "init", "-q", "-b", "main")
    _cli("init", str(r))

    hook = r / ".git" / "hooks" / "pre-commit"
    foreign_top = "#!/bin/sh\n# our own lint gate\nruff check . || exit 1\n\n"
    foreign_bottom = "\n# and a trailer nobody may touch\necho done\n"
    old_block = (f"{BEGIN}\n"
                 f"# 1. what did this change invalidate?\n"
                 f'"/somewhere/old/memway" verify-change . 2>/dev/null | '
                 f'grep -A 99 "STALED KNOWLEDGE" || true\n'
                 f'"/somewhere/old/memway" index . --if-stale --quiet || true\n'
                 f"git add .coord 2>/dev/null || true\n"
                 f"{END}")
    hook.write_text(foreign_top + old_block + foreign_bottom)

    assert _cli("hooks", "install", str(r)).returncode == 0
    now = hook.read_text()

    assert now.startswith(foreign_top), "foreign content above the block moved"
    assert now.endswith(foreign_bottom), "foreign content below the block moved"
    assert now.count(BEGIN) == 1 and now.count(END) == 1, \
        f"install duplicated the block instead of replacing it:\n{now}"
    assert "COMMENT ROT" in now, \
        f"the upgraded block still cannot see a rot section:\n{now}"
    assert "/somewhere/old/memway" not in now, "stale exe path survived"


def test_the_hook_block_greps_for_BOTH_report_sections():
    """Derived, not restated: the sections the hook looks for must be the
    sections the printer emits. A grep naming one of them makes the other
    invisible - which is exactly what 0.55.4 had to widen."""
    from memway import hooks
    from pathlib import Path
    block = hooks.pre_commit_block_for("memway")
    cli_src = (Path(__file__).resolve().parent.parent
               / "memway" / "cli.py").read_text()
    for section in ("STALED KNOWLEDGE", "COMMENT ROT"):
        assert section in cli_src, \
            f"the printer no longer emits {section!r}"
        assert section in block, \
            f"the pre-commit hook cannot see {section!r}:\n{block}"
