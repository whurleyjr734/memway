"""Map freshness: the check, the hooks, and the warning that backs them.

The lag this exists for was measured on memway itself: seven commits, three
of which changed parsing, hashing and entity extraction, with the re-index
rule written down and followed by nobody. Discipline held for a while and
then quietly stopped, and nothing reported that it had.

THE WARNING IS THE GUARANTEE, not the hooks. Hooks cannot fire during a
bisect, in a fresh worktree, on a hand-edited tree, or anywhere nobody ran
`hooks install`. So the tests that matter most here are the ones asserting
a lagging map SAYS SO on every read.

A HOOK MUST NEVER BREAK A COMMIT. Asserted directly, by breaking the index
and committing anyway.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import freshness, hooks
from memway.hooks import BEGIN, END, HOOKS, plan, strip_block


def _git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, **kw)


def _commit(repo, msg="c"):
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=T",
         "commit", "-qm", msg, "--no-gpg-sign")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _cli(*args, cwd=None):
    return subprocess.run([sys.executable, "-m", "memway.cli", *args],
                          capture_output=True, text=True, cwd=cwd or str(HERE))


@pytest.fixture
def repo(tmp_path):
    R = tmp_path / "proj"
    R.mkdir()
    _git(R, "init", "-q", "-b", "main")
    (R / "m.py").write_text('def alpha(x):\n    """D."""\n    return x + 1\n')
    _commit(R, "one")
    r = _cli("init", str(R))
    assert r.returncode == 0, r.stderr[-400:]
    return R


# --------------------------------------------------------- the recording

def test_index_records_the_sha_it_built_from(repo):
    man = json.loads((repo / ".coord" / "manifest.json").read_text())
    assert man[freshness.SHA_KEY] == _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert man[freshness.DIRTY_KEY] is False


def test_recording_is_additive(repo):
    """A manifest keeps whatever it already had. Older maps stay readable."""
    man = json.loads((repo / ".coord" / "manifest.json").read_text())
    assert man.get("format") and man.get("language"), man


def test_a_map_with_no_recorded_sha_reports_nothing(repo):
    """Maps written before this feature must not be called stale. Silence
    beats a guess: a warning that fires when it cannot know is noise, and
    noise is how a real warning gets ignored."""
    p = repo / ".coord" / "manifest.json"
    man = json.loads(p.read_text())
    del man[freshness.SHA_KEY]
    p.write_text(json.dumps(man))
    assert freshness.lag(repo, repo / ".coord") == {}


def test_outside_a_git_repo_nothing_is_claimed(tmp_path):
    assert freshness.head_sha(tmp_path) == ""
    assert freshness.lag(tmp_path, tmp_path / ".coord") == {}


# ------------------------------------------------------------- detection

def test_current_map_is_not_stale(repo):
    assert freshness.lag(repo, repo / ".coord") == {}


def test_a_new_commit_makes_the_map_behind(repo):
    (repo / "m.py").write_text("def alpha(x):\n    return x + 2\n")
    _commit(repo, "two")
    gap = freshness.lag(repo, repo / ".coord")
    assert gap and gap["behind"] == 1
    assert "HEAD is" in gap["message"] and "run memway index" in gap["message"]


def test_a_dirty_tree_counts_as_stale(repo):
    (repo / "m.py").write_text("def alpha(x):\n    return x + 3\n")
    gap = freshness.lag(repo, repo / ".coord")
    assert gap and gap["dirty"] is True
    assert "uncommitted changes" in gap["message"]


def test_coord_itself_is_not_dirt(repo):
    """THE loop this prevents: memway tells you to commit the map, so an
    index modifies a tracked path by definition. Counting that made
    --if-stale see a dirty tree right after a successful index, reindex,
    dirty the tree, forever. Measured before the exclusion went in."""
    _commit(repo, "track the map")          # HEAD moves: legitimately behind
    _cli("index", str(repo), "--if-stale")   # catches up, dirtying .coord
    # THE LOOP: .coord is now modified against HEAD. If that counted as
    # dirt, this second call would reindex, dirty it again, and never
    # settle. It must report current instead.
    r = _cli("index", str(repo), "--if-stale")
    assert "map current" in r.stdout, r.stdout
    assert not freshness.is_dirty(repo), "the map describing itself is not dirt"
    again = _cli("index", str(repo), "--if-stale")
    assert "map current" in again.stdout, "not idempotent"


def test_untracked_files_are_not_dirt(repo):
    (repo / "scratch.txt").write_text("notes to self\n")
    assert freshness.lag(repo, repo / ".coord") == {}


# ------------------------------------------------------------ --if-stale

def test_if_stale_is_a_read_when_the_map_is_current(repo):
    """Runs on every commit once hooks are in. The current path must not
    write, which is what keeps it inside the read fence."""
    import hashlib
    def fp():
        return {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((repo / ".coord").rglob("*"))
                if p.is_file() and "log" not in p.parts}
    before = fp()
    r = _cli("index", str(repo), "--if-stale", "--quiet")
    assert r.returncode == 0
    assert fp() == before, "--if-stale wrote to a current map"


def test_if_stale_reindexes_when_behind(repo):
    (repo / "m.py").write_text("def alpha(x):\n    return x + 4\n\n\ndef beta():\n    pass\n")
    _commit(repo, "two")
    r = _cli("index", str(repo), "--if-stale")
    assert r.returncode == 0
    assert "reindexing" in r.stdout, r.stdout
    assert freshness.lag(repo, repo / ".coord") == {}, "still stale after reindex"


def test_quiet_says_nothing_when_current(repo):
    """Silent commits stay silent."""
    r = _cli("index", str(repo), "--if-stale", "--quiet")
    assert r.stdout.strip() == "", f"quiet emitted: {r.stdout!r}"


def test_quiet_says_one_line_when_it_worked(repo):
    (repo / "m.py").write_text("def alpha(x):\n    return x + 5\n")
    _commit(repo, "two")
    r = _cli("index", str(repo), "--if-stale", "--quiet")
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"expected one line, got {lines}"
    assert lines[0].startswith("memway: map reindexed at")


def test_if_stale_without_a_map_does_not_explode(tmp_path):
    R = tmp_path / "nomap"
    R.mkdir()
    _git(R, "init", "-q", "-b", "main")
    (R / "a.py").write_text("x = 1\n")
    _commit(R, "one")
    r = _cli("index", str(R), "--if-stale")
    assert r.returncode == 0, r.stderr[-300:]
    assert "run memway init" in r.stdout


def test_if_stale_outside_git_exits_zero(tmp_path):
    """Hooks must never break a commit, and this must never be the reason
    a non-git checkout fails."""
    R = tmp_path / "nogit"
    R.mkdir()
    (R / "a.py").write_text("x = 1\n")
    assert _cli("init", str(R)).returncode == 0
    r = _cli("index", str(R), "--if-stale")
    assert r.returncode == 0
    assert "not a git repository" in r.stdout


def test_an_unknown_flag_is_rejected(repo):
    r = _cli("index", str(repo), "--nope")
    assert r.returncode != 0
    assert "unknown flag" in (r.stdout + r.stderr)


# ---------------------------------------------------------- the warning

def test_every_read_surface_reports_a_lagging_map(repo):
    """THE guarantee. Hooks are a convenience; this is the promise."""
    from memway import query
    (repo / "m.py").write_text("def alpha(x):\n    return x + 6\n")
    _commit(repo, "two")
    for name, call in (("summary", lambda: query.summary(str(repo))),
                       ("show", lambda: query.show(str(repo), "m.alpha")),
                       ("before_edit", lambda: query.before_edit(str(repo), "m.alpha"))):
        out = call()
        assert out.get("map_lag"), f"{name} served a stale map silently"
        assert "run memway index" in out["map_lag"]["message"]


def test_before_edit_puts_it_in_warnings_too(repo):
    """before_edit is the briefing read before touching code. A stale map
    there is the difference between 'no callers' and 'no callers as of
    seven commits ago'."""
    from memway import query
    (repo / "m.py").write_text("def alpha(x):\n    return x + 7\n")
    _commit(repo, "two")
    out = query.before_edit(str(repo), "m.alpha")
    assert any("run memway index" in w for w in out["warnings"]), out["warnings"]


def test_a_current_map_says_nothing(repo):
    """The other half. A warning that always fires is not a warning."""
    from memway import query
    for out in (query.summary(str(repo)), query.show(str(repo), "m.alpha")):
        assert out["map_lag"] == {}


def test_the_cli_prints_the_note(repo):
    (repo / "m.py").write_text("def alpha(x):\n    return x + 8\n")
    _commit(repo, "two")
    r = _cli("show", str(repo), "m.alpha")
    assert "note: map indexed at" in r.stdout, r.stdout


# -------------------------------------------------------------- hooks

def test_install_writes_all_three_executable(repo):
    r = _cli("hooks", "install", str(repo))
    assert r.returncode == 0, r.stderr[-300:]
    for name in HOOKS:
        p = repo / ".git" / "hooks" / name
        assert p.exists(), f"{name} missing"
        assert os.access(p, os.X_OK), f"{name} not executable"
        assert BEGIN in p.read_text() and END in p.read_text()


def test_hook_body_is_posix_and_short():
    """A hook nobody can read is a hook nobody trusts."""
    body = hooks.BLOCK
    assert len(body.splitlines()) <= 5, body
    for bashism in ("[[", "declare ", "local ", "function ", "=~"):
        assert bashism not in body, bashism


def test_append_preserves_a_foreign_hook_byte_identically(repo):
    p = repo / ".git" / "hooks" / "post-commit"
    p.parent.mkdir(parents=True, exist_ok=True)
    mine = '#!/bin/sh\necho "mine"\n'
    p.write_text(mine)
    _cli("hooks", "install", str(repo))
    assert 'echo "mine"' in p.read_text()
    _cli("hooks", "uninstall", str(repo))
    assert p.read_text() == mine, "round-trip was not byte-identical"


def test_a_block_is_never_appended_after_an_exit(repo):
    """THE silent no-op: git's own sample hooks end in `exit 0`, and a
    block appended past one installs cleanly, reports success and never
    runs. Measured on a fixture before this was handled."""
    p = repo / ".git" / "hooks" / "post-commit"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('#!/bin/sh\necho "mine"\nexit 0\n')
    _cli("hooks", "install", str(repo))
    lines = [l.strip() for l in p.read_text().splitlines() if l.strip()]
    assert lines.index(BEGIN) < lines.index("exit 0"), \
        f"memway block is unreachable dead code:\n{p.read_text()}"


def test_a_hook_that_exits_partway_is_refused(repo):
    p = repo / ".git" / "hooks" / "post-commit"
    p.parent.mkdir(parents=True, exist_ok=True)
    mine = '#!/bin/sh\necho a\nexit 0\necho b\nexit 1\n'
    p.write_text(mine)
    r = _cli("hooks", "install", str(repo))
    assert "REFUSING" in r.stdout
    assert p.read_text() == mine, "a refused hook was modified"


def test_a_hook_without_a_shebang_is_refused(repo):
    p = repo / ".git" / "hooks" / "post-merge"
    p.parent.mkdir(parents=True, exist_ok=True)
    mine = "not a script\n"
    p.write_text(mine)
    r = _cli("hooks", "install", str(repo))
    assert "REFUSING" in r.stdout
    assert p.read_text() == mine


def test_uninstall_removes_only_our_block(repo):
    _cli("hooks", "install", str(repo))
    for name in HOOKS:
        assert (repo / ".git" / "hooks" / name).exists()
    _cli("hooks", "uninstall", str(repo))
    for name in HOOKS:
        p = repo / ".git" / "hooks" / name
        assert not p.exists() or BEGIN not in p.read_text()


def test_install_is_idempotent(repo):
    _cli("hooks", "install", str(repo))
    first = (repo / ".git" / "hooks" / "post-commit").read_text()
    r = _cli("hooks", "install", str(repo))
    assert "already has the memway block" in r.stdout
    assert (repo / ".git" / "hooks" / "post-commit").read_text() == first


def test_strip_block_is_exact():
    text = f"#!/bin/sh\necho keep\n\n{BEGIN}\nmemway index . --if-stale\n{END}\n"
    assert strip_block(text).strip() == "#!/bin/sh\necho keep"
    assert strip_block("#!/bin/sh\necho keep\n") == "#!/bin/sh\necho keep\n"


def test_a_broken_hook_never_blocks_a_commit(repo):
    """Asserted by breaking it and committing anyway. Somebody whose
    commit was blocked by an indexing tool removes the tool, correctly."""
    _cli("hooks", "install", str(repo))
    p = repo / ".git" / "hooks" / "post-commit"
    p.write_text(p.read_text().replace("memway index . --if-stale --quiet",
                                       "definitely-not-a-command --boom"))
    (repo / "m.py").write_text("def alpha(x):\n    return x + 9\n")
    _git(repo, "add", "-A")
    r = _git(repo, "-c", "user.email=t@t", "-c", "user.name=T",
             "commit", "-m", "survives", "--no-gpg-sign")
    assert r.returncode == 0, f"the hook blocked the commit: {r.stderr}"
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "survives"


def test_setup_advertises_hooks_without_installing_them(repo):
    """Opt-in is the brand. A tool that writes into .git/hooks uninvited
    has taken something it was not offered."""
    r = _cli("setup", str(repo))
    assert "memway hooks install" in r.stdout
    for name in HOOKS:
        assert not (repo / ".git" / "hooks" / name).exists(), \
            f"setup installed {name} without being asked"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores chmod")
def test_an_index_that_raises_is_logged_and_exits_zero(repo):
    """The OTHER failure path. `|| true` in the hook covers a shell-level
    failure; this covers an exception INSIDE memway, which would otherwise
    print a traceback over somebody's commit output.

    Found by falsification: narrowing the `except` did not fail any test,
    because the only broken-hook test breaks the command name and never
    reaches Python at all.
    """
    (repo / "m.py").write_text("def alpha(x):\n    return x + 11\n")
    _commit(repo, "two")
    idx = repo / ".coord" / "index"
    mode = idx.stat().st_mode
    os.chmod(idx, 0o000)
    try:
        r = _cli("index", str(repo), "--if-stale", "--quiet")
    finally:
        os.chmod(idx, mode)
    assert r.returncode == 0, f"a failing index must still exit 0: {r.stderr[-200:]}"
    assert "reindex failed" in r.stdout
    assert "commit unaffected" in r.stdout
    assert "Traceback" not in (r.stdout + r.stderr), "a traceback reached the user"
    log = repo / ".coord" / "log" / "hooks.log"
    assert log.exists(), "the failure was not logged"
    assert "index --if-stale failed" in log.read_text()


# ------------------------------------- committing the map is not a change
#
# THE BUG THESE EXIST FOR. memway tells you to commit .coord, so committing
# the map moves HEAD - and lag() gated on `was == sha`, so the warning fired
# permanently the moment anyone followed the advice. It shipped in 0.53.0
# and 0.53.1 and was found by looking at the tool's own repo.
#
# is_dirty() had already learned to exclude .coord. The behind-count had
# not. Two copies of one rule, one of them fixed. There is now a single
# counting implementation, code_commits_between, and these tests pin the
# workflow rather than the mechanism.

def test_committing_the_map_leaves_it_current(repo):
    """THE regression. Commit .coord and nothing else: still current."""
    _commit(repo, "commit the map")
    assert freshness.lag(repo, repo / ".coord") == {}, \
        "committing the map reported the map as stale"


def test_committing_the_map_keeps_every_surface_silent(repo):
    from memway import query
    _commit(repo, "commit the map")
    assert query.summary(str(repo))["map_lag"] == {}
    assert query.show(str(repo), "m.alpha")["map_lag"] == {}
    r = _cli("show", str(repo), "m.alpha")
    assert "note: map indexed at" not in r.stdout, r.stdout
    r = _cli("index", str(repo), "--if-stale")
    assert "map current" in r.stdout, r.stdout


def test_a_source_commit_still_counts(repo):
    """The mirror. Without it, 'never warn' would pass the test above."""
    _commit(repo, "commit the map")
    (repo / "m.py").write_text("def alpha(x):\n    return x + 99\n")
    _commit(repo, "change the code")
    gap = freshness.lag(repo, repo / ".coord")
    assert gap, "a real source change went unreported"
    assert gap["behind"] == 1, f"counted map commits too: {gap}"


def test_a_mixed_commit_counts(repo):
    """Source and map in one commit still moved the code."""
    (repo / "m.py").write_text("def alpha(x):\n    return x + 5\n")
    _commit(repo, "source and map together")
    gap = freshness.lag(repo, repo / ".coord")
    assert gap and gap["behind"] == 1, gap


def test_many_map_commits_never_accumulate(repo):
    """Three map commits in a row must not read as three behind."""
    for i in range(3):
        (repo / ".coord" / "log").mkdir(parents=True, exist_ok=True)
        (repo / ".coord" / "log" / f"n{i}.txt").write_text("x")
        _commit(repo, f"map churn {i}")
    assert freshness.lag(repo, repo / ".coord") == {}


def test_an_unreachable_sha_reports_rather_than_going_silent(repo):
    """Rebase, force-push and shallow clones can orphan the recorded sha.
    'Cannot tell' is not 'nothing changed', and collapsing them would let a
    rewritten history read as current."""
    p = repo / ".coord" / "manifest.json"
    man = json.loads(p.read_text())
    man[freshness.SHA_KEY] = "0" * 40
    p.write_text(json.dumps(man))
    gap = freshness.lag(repo, repo / ".coord")
    assert gap, "an unreachable sha went silent"
    assert gap["known"] is False
    assert "cannot reach" in gap["message"]


def test_one_counting_implementation():
    """Structural: the rule that broke was a second copy of a first rule."""
    import ast
    src = (HERE / "memway" / "freshness.py").read_text()
    assert src.count('"rev-list"') == 1, \
        "a second rev-list appeared; unify it into code_commits_between"
    assert src.count(':(exclude).coord') == 2, \
        "both the dirty check and the behind count must exclude .coord"
