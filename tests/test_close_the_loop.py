"""Close the loop: the tool must say what you broke, when you broke it.

memway detected staleness perfectly and never mentioned it at the moment
it was caused. `before_edit` showed knowledge BEFORE an edit; the edit
invalidated it; `verify_change` - the step the rules send you to
afterwards - reported blast radius and tests and said nothing about the
notes now stale. Discovery was left to whoever later opened a map and
noticed a coral ring.

That is not hypothetical. Five notes on memway's own flagship map went
stale exactly this way during 0.54.0 and sat coral on the public site
until someone looked. The author had `before_edit` output in hand, and
followed all three workflow rules, and it still happened - which is the
argument that the process was incomplete rather than that the discipline
was poor.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import query

BEFORE = '''def widget(x):
    """Do the thing."""
    total = 0
    for i in x:
        total += i
    return total
'''

AFTER = '''def widget(x):
    """Do the thing."""
    total = 1
    for i in x:
        total += i * 2
    return total
'''

COSMETIC = '''def widget(x):
    """Do the thing, but with a longer docstring that changes nothing."""
    total = 0
    for i in x:
        total += i
    return total
'''

NOTE = "Accumulator starts at 0 deliberately - finance requires it."


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True)


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "memway.cli", *args],
                          capture_output=True, text=True, cwd=str(HERE))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    (r / "m.py").write_text(BEFORE)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "one", "--no-gpg-sign")
    assert _cli("init", str(r)).returncode == 0
    out = _cli("meta", str(r), "widget", "notes", NOTE)
    assert "added notes entry" in out.stdout, out.stdout + out.stderr
    return r


# --------------------------------------------------------- THE test

def test_the_precommit_trap_reports_from_the_working_tree(repo):
    """THE regression. Edited, NOT re-indexed, NOT committed.

    Staleness is computed against the INDEX, so at this exact moment the
    stored map still holds the old hashes and every stale-flag reads
    False. A check that consults the index here would report "nothing
    stale" at precisely the moment you needed the answer - the same shape
    as every other wrong-snapshot bug in this project's history.

    The contrast is the proof: `show` and `verify_change` are asked the
    same question about the same coordinate at the same instant, and they
    must disagree, because one reads the stored map and the other reads
    the tree.
    """
    (repo / "m.py").write_text(AFTER)
    assert _git(repo, "status", "--porcelain").stdout.strip(), \
        "fixture is not dirty - the trap needs uncommitted changes"

    shown = json.loads(_cli("--json", "show", str(repo), "widget").stdout)
    assert [k["stale"] for k in shown["knowledge"]] == [False], \
        "the stored index unexpectedly knows about the edit; this test no " \
        "longer exercises the trap"

    vc = json.loads(_cli("--json", "verify-change", str(repo)).stdout)
    staled = vc["staled_knowledge"]
    assert len(staled) == 1, vc
    assert staled[0]["text"] == NOTE
    assert staled[0]["qualname"] == "m.widget"


def test_the_report_names_the_channel(repo):
    """Channel is REQUIRED, not decorative. Superseding only heals when the
    fresh entry lands in the SAME channel - a confirm does not answer a
    stale note. A report that omits it sends the reader to write an entry
    that changes nothing, which is worse than no report."""
    (repo / "m.py").write_text(AFTER)
    vc = json.loads(_cli("--json", "verify-change", str(repo)).stdout)
    entry = vc["staled_knowledge"][0]
    assert set(entry) == {"coordinate", "qualname", "channel", "text"}, entry
    assert entry["channel"] == "notes"
    assert entry["coordinate"].startswith("C-")


def test_a_cosmetic_edit_stales_nothing(repo):
    """The other half. If every edit reported staleness the report would be
    noise, and noise is how a real warning gets ignored. Docstring-only
    changes must come back empty."""
    (repo / "m.py").write_text(COSMETIC)
    vc = json.loads(_cli("--json", "verify-change", str(repo)).stdout)
    assert vc["staled_knowledge"] == [], vc


def test_superseded_entries_are_not_re_reported(repo):
    """Only the NEWEST entry per channel counts - the ring rule, one
    implementation, shared with viz. Entries are append-only, so without
    this every past answer resurfaces forever and the report grows
    monotonically until nobody reads it."""
    (repo / "m.py").write_text(AFTER)
    _cli("index", str(repo))
    out = _cli("meta", str(repo), "widget", "notes",
               "Re-checked after the change: still deliberate.")
    assert "added notes entry" in out.stdout, out.stdout

    (repo / "m.py").write_text(AFTER.replace("i * 2", "i * 3"))
    vc = json.loads(_cli("--json", "verify-change", str(repo)).stdout)
    texts = [e["text"] for e in vc["staled_knowledge"]]
    assert len(texts) == 1, texts
    assert texts[0].startswith("Re-checked"), \
        f"the superseded entry was re-reported: {texts}"


def test_the_ring_rule_has_one_implementation():
    """viz asks 'draw a ring?', verify_change asks 'which entries?'. Two
    copies of that rule is how the behind-count shipped without the
    exclusion the dirty check already had."""
    import ast
    src = (HERE / "memway" / "viz.py").read_text()
    assert "from .metadata import unsuperseded_stale" in src, \
        "viz no longer delegates the ring rule"
    tree = ast.parse((HERE / "memway" / "viz.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "has_unsuperseded_stale":
            body = ast.dump(node)
            assert "unsuperseded_stale" in body
            assert "newest" not in body, "viz reimplemented the rule inline"


# ------------------------------------------------- attention, 3 of 3

def test_attention_reaches_all_three_surfaces():
    """It was MCP-only: not a --json query, and `memway attention` printed
    the usage banner. Every release in this project is driven from the
    CLI, so the one question that finds staled knowledge repo-wide could
    not be asked by its own author."""
    from memway import mcp
    from memway.cli import COMMANDS
    assert "attention" in query.QUERIES, "not a --json query"
    assert "attention" in COMMANDS, "not a CLI command"
    names = {t["name"] for t in mcp.TOOLS}
    assert "memway_attention" in names, "not an MCP tool"


def test_attention_has_one_implementation():
    """The stamp_for pattern. Three surfaces, one function."""
    import ast
    tree = ast.parse((HERE / "memway" / "cli.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_attention":
            dump = ast.dump(node)
            assert "attention" in dump
            assert "comment_rot" in dump or "query" in dump
            # it must CALL query.attention, not recompute the queue
            assert "MetaStore" not in dump, "cmd_attention rebuilds the queue"
            return
    pytest.fail("cmd_attention not found")


def test_attention_is_a_pure_read(repo):
    """It goes through _ctx, which never warms a cache - but it is a query
    now, so the fence covers it and this states the expectation locally."""
    import hashlib
    def fp():
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((repo / ".coord").rglob("*")) if p.is_file()}
    (repo / "m.py").write_text(AFTER)
    # First call establishes the docbindings SNAPSHOT BASELINE, which is
    # deliberate: suppressing that write unconditionally would make every
    # binding read permanently fresh (harvest.py says so, and two tests
    # caught it once). The fence uses the same convention. Everything
    # AFTER the baseline must be inert.
    _cli("--json", "attention", str(repo))
    before = fp()
    _cli("--json", "attention", str(repo))
    assert fp() == before


# ------------------------------------------------------ the fourth rule

def test_the_fourth_rule_is_emitted_to_all_three_files(tmp_path):
    from memway.cli import RULE_FILES
    r = tmp_path / "fresh"
    r.mkdir()
    (r / "m.py").write_text("x = 1\n")
    _git(r, "init", "-q", "-b", "main")
    assert _cli("setup", str(r)).returncode == 0
    bodies = {}
    for name in RULE_FILES:
        p = r / name
        assert p.exists(), f"{name} not written"
        bodies[name] = p.read_text()
        assert "supersede it before you finish" in bodies[name], name
        assert "same channel" in bodies[name], name
    assert len(set(bodies.values())) == 1, "the three files are not identical"


def test_the_fourth_rule_arrives_by_upgrade_not_clobber(tmp_path):
    """An existing marked block is UPGRADED and anything the user wrote
    below it survives. Rewriting somebody's CLAUDE.md is the same trespass
    as rewriting their git hook."""
    from memway.cli import managed_block
    r = tmp_path / "existing"
    r.mkdir()
    (r / "m.py").write_text("x = 1\n")
    _git(r, "init", "-q", "-b", "main")
    _cli("setup", str(r))

    p = r / "AGENTS.md"
    doc = p.read_text()
    i = doc.index("- If your change staled knowledge")
    j = doc.index("history.", i) + len("history.\n")
    p.write_text(doc[:i] + doc[j:] + "\n## My own notes\n\nKeep this line.\n")
    assert "supersede it before you finish" not in p.read_text()

    assert _cli("setup", str(r)).returncode == 0
    after = p.read_text()
    assert "supersede it before you finish" in after, "rule not restored"
    assert "Keep this line." in after, "the user's own text was clobbered"
    # the managed region matches the other files even though this one has a tail
    assert managed_block(after) == managed_block((r / "GEMINI.md").read_text())


# --------------------------------------------------------- the riders

def test_an_ambiguous_ref_is_not_reported_as_absent():
    """'no entity matches' when five do is a false negative that sends the
    caller to grep. Measured on the published 0.54.0 wheel: `get_signature`
    matched 5 entities in itsdangerous and `save` matches 3 here."""
    from memway.indexer import Indexer
    ix = Indexer(HERE, HERE / ".coord")
    ix.load_existing(write_cache=False)
    n = sum(1 for q in ix.by_qualname if q.rsplit(".", 1)[-1] == "save")
    assert n > 1, f"fixture assumption broken: only {n} entities named .save"

    err = query._resolve_error("save", ix)
    assert "ambiguous" in err["error"], err
    assert str(n) in err["error"], err
    assert len(err["matches"]) == n
    assert all(m.endswith(".save") for m in err["matches"]), err

    absent = query._resolve_error("zzz_no_such_entity", ix)
    assert "no entity matches" in absent["error"], absent
    assert "matches" not in absent, "an absent ref must not claim candidates"


def test_a_failed_lookup_exits_nonzero(repo):
    """It returned 0, so a script could not tell a miss from a hit."""
    r = _cli("show", str(repo), "zzz_no_such_entity")
    assert r.returncode != 0, r.stdout


def test_the_console_banner_survives_redirection(tmp_path):
    """The token exists nowhere else, and Python block-buffers a
    non-TTY stdout while the server blocks in serve_forever - so
    `memway console > log` produced zero bytes with the server up.
    Executed, because the bug was invisible to any reading of the source.
    """
    r = tmp_path / "c"
    r.mkdir()
    (r / "m.py").write_text("x = 1\n")
    _git(r, "init", "-q", "-b", "main")
    _cli("init", str(r))
    log = tmp_path / "out.log"
    with log.open("wb") as fh:
        p = subprocess.Popen(
            [sys.executable, "-m", "memway.cli", "console", str(r),
             "--port", "8837"],
            stdout=fh, stderr=subprocess.STDOUT, cwd=str(HERE))
        try:
            deadline = time.time() + 15
            while time.time() < deadline and log.stat().st_size == 0:
                time.sleep(0.25)
        finally:
            p.terminate()
            p.wait(timeout=10)
    text = log.read_text()
    assert "memway console on" in text, f"banner never flushed: {text!r}"
    assert "token=" in text, text
