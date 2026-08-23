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
import shlex
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
    # THE PROPERTY, not the spelling. This matched the literal string
    # "from .metadata import unsuperseded_stale" and broke the moment viz
    # collapsed its four scattered metadata imports into one parenthesised
    # module-level import - a refactor that STRENGTHENED the delegation it
    # was guarding. A pin that fails on correct code gets deleted.
    imported = any(
        isinstance(n, ast.ImportFrom) and n.module == "metadata"
        and any(a.name == "unsuperseded_stale" for a in n.names)
        for n in ast.walk(ast.parse(src)))
    assert imported, "viz no longer delegates the ring rule"
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
        # Substance, not prose: the rule was reworded in 0.54.2 when the
        # ambient warning made "remember to ask" obsolete, and a test
        # pinned to a sentence blocks its own improvement.
        low = bodies[name].lower()
        assert "supersede" in low, name
        assert "same channel" in low, name
        assert "verify_change" in low or "verify-change" in low, name
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
    # Anchored on the rule's stable words, not its prose: 0.54.2 reworded
    # it when the ambient warning made "remember to ask" obsolete.
    i = doc.index("- When you are told knowledge has gone stale")
    j = doc.index("decides.", i) + len("decides.\n")
    p.write_text(doc[:i] + doc[j:] + "\n## My own notes\n\nKeep this line.\n")
    assert "supersede" not in p.read_text(), "forge failed - rule still there"

    assert _cli("setup", str(r)).returncode == 0
    after = p.read_text()
    assert "supersede" in after, "rule not restored"
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


# ------------------------------------------- the queue tells the truth

def test_attention_counts_the_decisive_queue_not_the_history(repo):
    """A superseded stale entry is history, not a warning - on EVERY
    surface that counts.

    attention hand-rolled `en.get("stale")` across all entries, so the
    flagship advertised 43 stale entries when 3 needed answering: the
    other 40 were entries somebody had already replaced, with the
    replacement sitting directly above in the same channel. Ambient
    _knowledge_lag, reading the same bytes through unsuperseded_stale,
    said 3 the whole time. Two surfaces, one repo, one number, two
    answers - and the one people are sent to was the wrong one.

    Fixture shape matters: one coordinate carrying BOTH a superseded
    stale entry and a live one. A fixture with only live entries passes
    against the hand count too, which is the version of this test that
    would have proved nothing.
    """
    # stale the original note by changing behaviour
    (repo / "m.py").write_text(AFTER)
    assert _cli("index", str(repo)).returncode == 0

    before = query.attention(str(repo))["stale_notes"]
    assert before == 1, f"one stale note expected, got {before}"

    # answer it: a second entry in the same channel supersedes the first
    out = _cli("meta", str(repo), "widget", "notes", "answered: still correct")
    assert "added notes entry" in out.stdout, out.stdout + out.stderr

    after = query.attention(str(repo))["stale_notes"]
    raw = _raw_stale_count(repo)
    assert raw == 1, f"the superseded entry must still be ON DISK, got {raw}"
    assert after == 0, (
        f"attention counted superseded history: {after} (raw flag count "
        f"is {raw} - that is the number the hand count reported)")


def test_the_queue_and_the_ambient_warning_agree(repo):
    """Same rule, same repo, same verdict on whether anything is due.

    Not the same integer: attention counts ENTRIES, ambient counts
    COORDINATES, and a coordinate can hold two decisive entries. What
    must never differ is whether they see a queue at all - that is the
    contradiction that made the flagship read 43-and-silent at once.
    """
    from memway.query import _ctx, _knowledge_lag

    (repo / "m.py").write_text(AFTER)
    assert _cli("index", str(repo)).returncode == 0

    def both():
        _, _, ix, _, meta = _ctx(str(repo))
        return (query.attention(str(repo))["stale_notes"] > 0,
                bool(_knowledge_lag(ix, meta)))

    q, ambient = both()
    assert q and ambient, f"stale note invisible: queue={q} ambient={ambient}"

    _cli("meta", str(repo), "widget", "notes", "answered: still correct")
    q, ambient = both()
    assert not q and not ambient, (
        f"answered, but queue={q} ambient={ambient} - the surfaces disagree")


def _raw_stale_count(repo) -> int:
    """What the hand count reported: every entry with the flag set."""
    from memway.metadata import MetaStore, accepted_for
    from memway.query import _ctx
    _, _, ix, _, meta = _ctx(str(repo))
    n = 0
    for e in ix.entities.values():
        md = meta.read_all(e.coord_id, current_hash=accepted_for(e))
        n += sum(1 for ens in md.values() for en in ens if en.get("stale"))
    return n


# ------------------------------------------ comment rot rides the commit

ROT_BEFORE = '''def widget(x):
    """Sums the list by looping."""
    # walks each element and accumulates
    total = 0
    for i in x:
        total += i
    return total


def elsewhere(y):
    """Doubles it."""
    # this one is never touched
    return y * 2
'''

ROT_AFTER = '''def widget(x):
    """Sums the list by looping."""
    # walks each element and accumulates
    return sum(x)


def elsewhere(y):
    """Doubles it."""
    # this one is never touched
    return y * 2
'''


def _rot_repo(tmp_path):
    """A repo carrying PRE-EXISTING rot, so scoping can be tested.

    elsewhere is rotted first and left alone. Any later change must NOT
    re-report it - a fixture with only the freshly-rotted entity would
    pass a scoping bug that dumped the whole backlog.
    """
    r = tmp_path / "proj"
    r.mkdir()
    (r / "m.py").write_text(ROT_BEFORE)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "one", "--no-gpg-sign")
    assert _cli("init", str(r)).returncode == 0
    # rot `elsewhere` and BANK it into the map: logic moves, comment does not
    (r / "m.py").write_text(ROT_BEFORE.replace("return y * 2",
                                               "return y + y"))
    assert _cli("index", str(r)).returncode == 0
    return r


def _stale_widget(r, marker="or 0"):
    """Move widget's LOGIC and bank it, so entries written before now stale.

    Four tests used to write a confirm and immediately affirm it, which
    reads naturally and is a no-op: nothing had gone stale, so there was
    nothing to re-stamp. They passed only because `affirm` used to write
    unconditionally. A test that exercises a re-stamp has to earn one.
    """
    src = (r / "m.py").read_text()
    # widget is in one of two shapes depending on what the caller wrote
    if "return sum(x)" in src:
        new = src.replace("return sum(x)", f"return sum(x) {marker}", 1)
    else:
        assert "total += i" in src, f"[fixture] unexpected widget:\n{src}"
        new = src.replace("total += i", "total += int(i)", 1)
    assert new != src, "[fixture] no logic change applied - nothing will stale"
    (r / "m.py").write_text(new)
    assert _cli("index", str(r)).returncode == 0


def test_a_change_reports_the_comments_IT_rotted(tmp_path):
    """Caught at the commit that causes it, while the author still has
    the reasoning in their head - the same moment, and the same scoping,
    as staled_knowledge."""
    r = _rot_repo(tmp_path)
    backlog = query.attention(str(r))["comment_rot_total"]
    assert backlog >= 1, "fixture did not bank any pre-existing rot"

    (r / "m.py").write_text(ROT_AFTER.replace("return y * 2",
                                              "return y + y"))
    v = query.verify_change(str(r))
    names = {e["qualname"] for e in v["rotted_comments"]}
    assert any(n.endswith("widget") for n in names), (
        f"the change rotted widget's comments and said nothing: {names}")


def test_it_does_NOT_report_the_pre_existing_backlog(tmp_path):
    """THE SCOPING HALF. attention read 43 when 3 were actionable and
    stopped being worked; a commit-time alarm that fires on other
    people's rot dies the same way, faster."""
    r = _rot_repo(tmp_path)
    (r / "m.py").write_text(ROT_AFTER.replace("return y * 2",
                                              "return y + y"))
    v = query.verify_change(str(r))
    names = {e["qualname"] for e in v["rotted_comments"]}
    assert not any(n.endswith("elsewhere") for n in names), (
        f"the backlog leaked into the commit report: {names}. Scope to "
        f"changed_ids; the backlog belongs to `memway attention`.")


def test_a_current_confirm_answers_rot_at_commit_time(tmp_path):
    """Same suppression the queue uses, one implementation."""
    r = _rot_repo(tmp_path)
    (r / "m.py").write_text(ROT_AFTER.replace("return y * 2",
                                              "return y + y"))
    # NO index here. verify_change reports what moved against the STORED
    # map, so re-indexing first empties changed_ids and the report goes
    # quiet - which is the same ordering trap CLAUDE.md lesson 9 records
    # for staled_knowledge, met here from the other side.
    before = {e["qualname"] for e in query.verify_change(str(r))["rotted_comments"]}
    out = _cli("meta", str(r), "widget", "confirm",
               "read it: logic moved to sum(), the comment still describes it")
    assert "added confirm entry" in out.stdout, out.stdout + out.stderr
    after = {e["qualname"] for e in query.verify_change(str(r))["rotted_comments"]}
    assert any(n.endswith("widget") for n in before), before
    assert not any(n.endswith("widget") for n in after), (
        f"a current confirm must answer rot: {after}")


def test_a_restamp_answers_rot_without_writing_prose(tmp_path):
    """THE CONFIRM-VOLUME FIX, end to end.

    Measured at 0.60.1: 176 of this repo's 257 entries were confirms,
    14,186 words of "I read it and it still holds" - because the only way
    to clear a flag was to WRITE that sentence. `affirm` re-stamps
    instead, and must clear exactly what a written confirm clears.
    """
    r = _rot_repo(tmp_path)
    (r / "m.py").write_text(ROT_AFTER.replace("return y * 2",
                                              "return y + y"))
    # the first attestation is prose, as it must be
    assert _cli("meta", str(r), "widget", "confirm",
                "read it: logic moved to sum(), the comment still fits"
                ).returncode == 0
    # ...the logic moves again, staling that confirm
    (r / "m.py").write_text(
        ROT_AFTER.replace("return sum(x)", "return sum(x) or 0")
                 .replace("return y * 2", "return y + y"))
    assert _cli("index", str(r)).returncode == 0
    (r / "m.py").write_text(
        ROT_AFTER.replace("return sum(x)", "return sum(x) or 0.0")
                 .replace("return y * 2", "return y + y"))
    before = {e["qualname"] for e in query.verify_change(str(r))["rotted_comments"]}
    assert any(n.endswith("widget") for n in before), (
        f"fixture banked no rot to answer: {before}")

    out = _cli("affirm", str(r), "widget")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "re-stamped" in out.stdout, out.stdout
    after = {e["qualname"] for e in query.verify_change(str(r))["rotted_comments"]}
    assert not any(n.endswith("widget") for n in after), (
        f"a re-stamp must answer rot exactly as a written confirm does: "
        f"{after}")

    # AND IT WROTE NO PROSE - the point of the exercise.
    import json as _json
    files = sorted((r / ".coord" / "meta").glob("*/confirm.jsonl"))
    assert len(files) == 1, f"fixture is ambiguous about which file to read: {files}"
    lines = [_json.loads(l) for l in files[0].read_text().splitlines()
             if l.strip()]
    assert [l for l in lines if l.get("reaffirms")], lines
    assert all(not l["text"] for l in lines if l.get("reaffirms")), (
        "a re-stamp that carries text is just a confirm with extra steps")


def test_a_restamp_does_not_bury_the_entry_it_vouches_for(tmp_path):
    """Supersession is POSITIONAL, so a textless entry taking a slot would
    mark the note it was vouching FOR as superseded history - and render
    as an empty note at the top of the panel. The worst of both."""
    from memway.metadata import MetaStore, for_display
    m = MetaStore(tmp_path)
    m.add("C-x", "confirm", "the threshold is 0.05 because rich needs it",
          body_hash="H1")
    m.reaffirm("C-x", "confirm", body_hash="H2")
    rows = for_display(m.read_all("C-x", "H2"))
    assert len(rows) == 1, f"the stamp leaked into the reading order: {rows}"
    assert rows[0]["text"].startswith("the threshold"), rows[0]
    assert not rows[0]["superseded"], (
        "the re-stamp superseded the very entry it was vouching for")
    assert rows[0].get("reaffirmed_by"), (
        "provenance lost: who vouched is the entire content of an "
        "attestation")


def test_the_first_attestation_must_be_prose(tmp_path):
    """THE GUARD THAT MAKES THIS HONEST RATHER THAN A MUTE BUTTON.

    A re-stamp over an empty channel would assert nothing while clearing
    a warning - which is the confirm fatigue this feature exists to end,
    arrived at from the other side. Somebody says it once; repeats are
    free.
    """
    r = _rot_repo(tmp_path)
    (r / "m.py").write_text(ROT_AFTER.replace("return y * 2",
                                              "return y + y"))
    before = {e["qualname"] for e in query.verify_change(str(r))["rotted_comments"]}
    assert any(n.endswith("widget") for n in before), before

    out = _cli("affirm", str(r), "widget")
    assert out.returncode != 0, (
        "affirming an empty channel must fail, not silently vouch for "
        "nothing: " + out.stdout)
    assert "nothing to reaffirm" in out.stdout, out.stdout
    after = {e["qualname"] for e in query.verify_change(str(r))["rotted_comments"]}
    assert after == before, f"the refused affirm still cleared rot: {after}"


def test_the_refusal_prints_a_remedy_THAT_RUNS(tmp_path):
    """Lesson 11, caught red-handed on this very feature.

    The refusal first read "write it with `meta --channel confirm`". That
    command does not exist - `meta` takes the channel POSITIONALLY, and
    `--channel` is `search`'s spelling - so the one line offered to a
    blocked user failed. The test beside it asserted only that the phrase
    "nothing to reaffirm" appeared, which a wrong remedy satisfies exactly
    as well as a right one.

    So this EXECUTES what the message prints. Parse the command out of
    stdout, run it, require success, and require that the affirm which
    was refused now works. A constant describing behaviour has to be
    pinned by execution or it is not pinned at all.
    """
    r = _rot_repo(tmp_path)
    out = _cli("affirm", str(r), "widget")
    assert out.returncode != 0, out.stdout

    line = [l for l in out.stdout.splitlines() if "memway meta" in l]
    assert line, f"the refusal offered no remedy at all:\n{out.stdout}"
    cmd = line[0].split("memway meta", 1)[1].strip()
    args = shlex.split(cmd)
    assert args and "<" not in args[0], f"remedy has an unfilled slot: {args}"
    # the last arg is the placeholder for the prose; supply real text
    args[-1] = "read it at this hash, the comment still describes the logic"

    ran = _cli("meta", *args)
    assert ran.returncode == 0, (
        f"the remedy the tool printed does not run:\n"
        f"  printed: memway meta {cmd}\n"
        f"  stdout : {ran.stdout}\n  stderr : {ran.stderr}")

    # and it must actually unblock the thing that was refused
    again = _cli("affirm", str(r), "widget")
    assert again.returncode == 0, (
        f"remedy ran but the refusal stands:\n{again.stdout}{again.stderr}")
    assert "nothing to reaffirm" not in again.stdout, again.stdout


def test_the_mcp_refusal_names_a_real_tool_and_a_real_parameter(tmp_path):
    """The OTHER door, and the reason the message had to split in two.

    An MCP caller has no shell, and the first version of this handed it a
    CLI command - wrong for the door and unusable by it. Its remedy names
    a tool and a parameter instead, so both are checked against the
    server's actual schema rather than read for plausibility.
    """
    from memway import mcp
    r = _rot_repo(tmp_path)
    res = query.agent_affirm(str(r), "widget", "confirm")
    assert "error" in res, res
    remedy = res.get("remedy", "")
    assert remedy, f"the MCP refusal offered no remedy: {res}"
    assert "memway meta " not in remedy, (
        f"the MCP door is offering a shell command: {remedy}")

    names = {t["name"] for t in mcp.TOOLS}
    named = [n for n in names if n in remedy]
    assert named, f"remedy names no tool this server advertises: {remedy}"
    tool = next(t for t in mcp.TOOLS if t["name"] == named[0])
    props = set(tool["inputSchema"]["properties"])
    assert "channel" in props, (
        f"remedy tells the caller to pass channel= to {named[0]}, whose "
        f"schema accepts {sorted(props)}")

    # and the remedy must actually unblock it, through the same door
    query.agent_meta(str(r), "widget", "confirm", "read it, still accurate")
    _stale_widget(r)
    ok = query.agent_affirm(str(r), "widget", "confirm")
    assert "error" not in ok, ok
    assert ok["reaffirmed"]["entries"] == 1, ok


def test_show_never_renders_a_stamp_as_a_blank_entry(tmp_path):
    """The panel bug, in the door that did not go through for_display.

    for_display drops re-stamps precisely so they cannot render as empty
    notes - and `memway show` walked the raw store instead, so it printed
    `<ts> (agent) ` with nothing after it. A second copy of the reading
    rule, found by running the command rather than by any test.
    """
    r = _rot_repo(tmp_path)
    assert _cli("meta", str(r), "widget", "confirm",
                "read it, the comment still describes the logic"
                ).returncode == 0
    _stale_widget(r)
    assert _cli("affirm", str(r), "widget").returncode == 0

    out = _cli("show", str(r), "widget")
    assert out.returncode == 0, out.stdout + out.stderr
    body = [l for l in out.stdout.splitlines()
            if l.strip().startswith("20")]      # entry lines start with a ts
    assert body, out.stdout
    for line in body:
        # everything after "<ts> (<author>)[flags]" must not be empty
        assert line.split(")", 1)[1].strip(), (
            f"a stamp rendered as a blank entry: {line!r}\n{out.stdout}")
    assert "re-stamped" in out.stdout, (
        f"the seal is stored but never shown:\n{out.stdout}")


def test_show_marks_superseded_entries_as_history(tmp_path):
    """Same second copy, older half. `show` had no notion of supersession
    at all, so the entry the ring DISCARDED read as a peer of the one that
    replaced it - the exact confusion for_display was written to end."""
    r = _rot_repo(tmp_path)
    for text in ("first reading of this", "second reading, replacing it"):
        assert _cli("meta", str(r), "widget", "notes", text).returncode == 0
    out = _cli("show", str(r), "widget")
    assert "SUPERSEDED" in out.stdout, (
        f"two notes, neither marked as history:\n{out.stdout}")
    # newest first: the replacement is above the entry it replaced
    i_new = out.stdout.index("second reading")
    i_old = out.stdout.index("first reading")
    assert i_new < i_old, f"file order, not reading order:\n{out.stdout}"


def test_the_json_door_carries_who_last_vouched(tmp_path):
    """An entry can be years old and freshly checked. Without the seal a
    reader cannot tell that from one nobody has revisited - both read
    `stale: false`."""
    r = _rot_repo(tmp_path)
    assert _cli("meta", str(r), "widget", "confirm", "read it").returncode == 0
    _stale_widget(r)
    assert _cli("affirm", str(r), "widget", "confirm",
                "--author", "quinn").returncode == 0
    d = query.before_edit(str(r), "widget")
    seals = [k for k in d["knowledge"] if k.get("reaffirmed_by")]
    assert seals, f"the whitelist dropped the seal: {d['knowledge']}"
    assert seals[0]["reaffirmed_by"] == "quinn", seals[0]


def test_affirming_something_already_current_writes_nothing(tmp_path):
    """THE VOLUME PROBLEM, REBUILT IN A CHEAPER FORMAT.

    Found by pointing the new tool at a fresh channel: five `affirm` calls
    on an unchanged coordinate left one claim and FIVE stamps, each
    recording that a hash which never moved still had not moved. An agent
    sweeping affirm across a repo would grow the store on every run - the
    exact disease this feature was built to cure, in a new costume.

    Nothing needed is a SUCCESS, not an error: a sweep must not fail on
    the coordinates that were already fine.
    """
    r = _rot_repo(tmp_path)
    assert _cli("meta", str(r), "widget", "confirm", "read it").returncode == 0

    def stamps():
        f = r / ".coord" / "meta"
        return sum(1 for p in f.glob("*/confirm.jsonl")
                   for l in p.read_text().splitlines()
                   if l.strip() and json.loads(l).get("reaffirms"))

    assert stamps() == 0
    for _ in range(5):
        out = _cli("affirm", str(r), "widget")
        assert out.returncode == 0, out.stdout + out.stderr
        assert "already current" in out.stdout, out.stdout
    assert stamps() == 0, (
        f"{stamps()} stamps written for a hash that never moved - the "
        f"store refills with entries recording nothing")

    # once the code actually moves, exactly ONE stamp answers it
    _stale_widget(r)
    assert "re-stamped" in _cli("affirm", str(r), "widget").stdout
    assert stamps() == 1
    assert "already current" in _cli("affirm", str(r), "widget").stdout
    assert stamps() == 1, "the repeat wrote a second stamp for the same hash"


def test_a_restamp_expires_when_the_logic_moves_again(tmp_path):
    """Not a silencer. It answers only until the code moves, which is what
    makes a written confirm honest, and must remain true of a stamp."""
    from memway.metadata import MetaStore, for_display, unsuperseded_stale
    m = MetaStore(tmp_path)
    m.add("C-x", "notes", "keep bare and via_attr separate", body_hash="H1")
    m.reaffirm("C-x", "notes", body_hash="H2")
    assert not unsuperseded_stale(for_display(m.read_all("C-x", "H2")))
    assert unsuperseded_stale(for_display(m.read_all("C-x", "H3"))), (
        "the stamp outlived the hash it was made against")


def test_no_reaffirmation_means_no_restamp(tmp_path):
    """THE ABSENT CASE, which is where this nearly shipped a hole.

    "index of the last accepted stamp, default -1" reads naturally and
    makes the slice out[:-1] when there is no stamp at all - clearing
    staleness on EVERY entry but the last, on every coordinate in the
    repo, forever. It would have looked like the feature working.
    """
    from memway.metadata import MetaStore, unsuperseded_stale, for_display
    m = MetaStore(tmp_path)
    m.add("C-x", "notes", "first", body_hash="H1")
    m.add("C-x", "notes", "second", body_hash="H1")
    m.add("C-x", "notes", "third", body_hash="H1")
    rows = m.read("C-x", "notes", "H2")
    assert all(e.get("stale") for e in rows), (
        f"an entry went fresh with nothing vouching for it: {rows}")
    assert len(unsuperseded_stale(for_display(m.read_all("C-x", "H2")))) == 1


def test_a_restamp_cannot_vouch_forward(tmp_path):
    """It attests to what was in front of the author. An entry written
    AFTER it is judged on its own stamp - nothing can vouch for text that
    did not exist when it was written."""
    from memway.metadata import MetaStore
    m = MetaStore(tmp_path)
    m.add("C-x", "notes", "older claim", body_hash="H1")
    m.reaffirm("C-x", "notes", body_hash="H2")
    m.add("C-x", "notes", "written after the stamp", body_hash="H2")
    rows = m.read("C-x", "notes", "H2")
    later = [e for e in rows if e["text"] == "written after the stamp"][0]
    assert not later.get("reaffirmed_by"), (
        "the stamp reached forward over an entry written after it")


def test_attention_reports_the_total_not_the_page(tmp_path):
    """No silent caps. This printed the length of the truncated slice as
    if it were the census - 20 against a real backlog of 49 - while
    markers shipped marker_total on the same line all along."""
    r = _rot_repo(tmp_path)
    a = query.attention(str(r), limit=1)
    assert "comment_rot_total" in a, sorted(a)
    assert len(a["comment_rot"]) <= 1, a["comment_rot"]
    assert a["comment_rot_total"] >= len(a["comment_rot"])
    full = query.attention(str(r), limit=10000)
    assert a["comment_rot_total"] == len(full["comment_rot"]), (
        f"total {a['comment_rot_total']} disagrees with the full list "
        f"{len(full['comment_rot'])}")


def test_module_rot_reaches_neither_the_commit_report_nor_the_queue(tmp_path):
    """0.56.1 ended module rot; this test used to assert the opposite.

    Written for 0.55.4, it required `attention` to STILL carry modules -
    correct then, because the filter was a routing decision: the commit
    alarm should not fire on something a confirm could never clear, but
    the queue should still list it.

    0.56.1 removed the flag at the source instead. A module docstring's
    claims range over the file and beyond it, so nothing bounds what the
    check is checking; the honest build is no check rather than an
    approximate one wearing a precise name. Modules now appear in
    neither surface, and this fixture asserts that rather than the
    routing it was born to protect.
    """
    r = _rot_repo(tmp_path)
    (r / "m.py").write_text(ROT_AFTER.replace("return y * 2",
                                              "return y + y"))
    v = query.verify_change(str(r))
    kinds = {e["qualname"]: e for e in v["rotted_comments"]}

    assert any(n.endswith("widget") for n in kinds), (
        f"the function rot must still fire: {sorted(kinds)}")
    assert not any(n == "m" or n.endswith(".m") for n in kinds), (
        f"a module rot reached the commit report: {sorted(kinds)}")

    # ...and the queue does not either, since 0.56.1
    assert _cli("index", str(r)).returncode == 0
    a = query.attention(str(r), limit=10000)
    assert not any(q == "m" or q.endswith(".m") for q in a["comment_rot"]), (
        f"a module reached the queue: {a['comment_rot']}. Module rot ends "
        f"at the computation now - see CLAUDE.md lesson 12.")


def _inherit_repo(tmp_path):
    r = tmp_path / "inh"; r.mkdir()
    (r / "m.py").write_text(
        "class Base:\n"
        "    def handle(self, x):\n"
        '        """Base handling."""\n'
        "        return x + 1\n"
        "\n\n"
        "class Child(Base):\n"
        "    def handle(self, x):\n"
        '        """Child handling."""\n'
        "        return x + 10\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "one", "--no-gpg-sign")
    assert _cli("init", str(r)).returncode == 0
    return r


def test_inherited_knowledge_marks_history_and_drops_stamps(tmp_path):
    """KNOWLEDGE FLOWING DOWN read the raw channels, and was wrong in both
    ways for_display exists to prevent.

    A re-stamp arrived as an inherited note with NO TEXT. And - long
    before stamps existed - an ancestor note somebody had explicitly
    SUPERSEDED arrived unmarked, in file order, AHEAD of the note that
    replaced it. The child was briefed with a retracted claim, presented
    as current, on the surface an agent reads before it edits.
    """
    r = _inherit_repo(tmp_path)
    assert _cli("meta", str(r), "m.Base.handle", "notes",
                "The +1 compensates for the zero-index.").returncode == 0
    # stale the ancestor, re-stamp it, then supersede it properly
    src = (r / "m.py").read_text()
    assert "return x + 1\n" in src
    (r / "m.py").write_text(src.replace("return x + 1\n", "return x + 2\n", 1))
    assert _cli("index", str(r)).returncode == 0
    assert _cli("affirm", str(r), "m.Base.handle", "notes").returncode == 0
    assert _cli("meta", str(r), "m.Base.handle", "notes",
                "SUPERSEDES: the zero-index story was wrong.").returncode == 0

    rows = [k for k in query.before_edit(str(r), "m.Child.handle")["knowledge"]
            if k.get("inherited_from")]
    assert rows, "the child inherits nothing - fixture proves nothing"
    assert all((k["text"] or "").strip() for k in rows), (
        f"a stamp arrived as a blank inherited note: {rows}")
    live = [k for k in rows if not k.get("superseded")]
    assert len(live) == 1, f"more than one entry presented as current: {rows}"
    assert "SUPERSEDES" in live[0]["text"], (
        f"the retracted claim is being presented as current: {live[0]}")
    assert rows[0]["text"] == live[0]["text"], (
        "file order, not reading order - the replaced note is on top")
