"""Reviewing knowledge, and the check that makes the loop a guarantee.

The hooks in this repo deliberately never block - `A HOOK MUST NEVER
BREAK A COMMIT`, because a hook that blocks gets uninstalled. CI is the
layer that can refuse without that failure mode, which is why the gate
lives behind a flag rather than in the pre-commit path.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def _git(r, *a):
    return subprocess.run(["git", "-C", str(r), *a],
                          capture_output=True, text=True)


def _cli(*a, cwd=None):
    return subprocess.run([sys.executable, "-m", "memway.cli", *[str(x) for x in a]],
                          capture_output=True, text=True, cwd=str(cwd or HERE))


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "m.py").write_text(
        'def alpha(x):\n    """Doc."""\n    return x + 1\n')
    _git(tmp_path, "init", "-q", "-b", "main")
    assert _cli("init", tmp_path).returncode == 0
    _cli("meta", tmp_path, "alpha", "notes", "The +1 is load-bearing.")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "base", "--no-gpg-sign")
    return tmp_path


def test_a_line_diff_cannot_show_supersession_but_review_can(repo):
    """THE reason this exists, asserted against git itself.

    The meta store is append-only, so superseding a note ADDS a line and
    leaves the superseded entry untouched. git therefore reports "1 +"
    and never shows what was replaced - not a formatting problem, a
    structural one: supersession is positional and a line diff compares
    lines.
    """
    # A REALISTIC CHANNEL DEPTH. With two entries git's three lines of
    # context happen to include the superseded one, which made the first
    # version of this test assert something false. Real coordinates in
    # this repo carry eight to eleven entries per channel, and there the
    # superseded entry is far outside any context window - which is the
    # situation the renderer exists for.
    for i in range(8):
        _cli("meta", repo, "alpha", "notes", f"interim observation {i}")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "depth", "--no-gpg-sign")
    _cli("meta", repo, "alpha", "notes", "SUPERSEDES: it is *99 now.")

    # what git can see
    diff = _git(repo, "diff", "--stat", "--", ".coord/meta").stdout
    assert "1 +" in diff or "1 insertion" in diff, diff
    body = _git(repo, "diff", "--", ".coord/meta").stdout
    assert "SUPERSEDES" in body, "the new entry should be in the diff"
    assert "The +1 is load-bearing" not in body, (
        "the original note is inside the diff's context window; deepen "
        "the fixture or this asserts nothing")

    # what review can see
    from memway.review import review
    r = review(str(repo), "HEAD")
    assert r["added_total"] == 1, r
    a = r["added"][0]
    assert "SUPERSEDES" in a["text"]
    assert a["supersedes"] and "interim observation 7" in a["supersedes"], (
        "review did not pair the new entry with the one it replaced, "
        "which is the only thing it exists to do")
    assert r["superseding"] == 1


def test_review_names_a_first_entry_as_first(repo):
    """A new channel has nothing behind it, and saying "supersedes null"
    would read as a lost link rather than an origin."""
    _cli("meta", repo, "alpha", "docs", "What this does and why.")
    from memway.review import review
    r = review(str(repo), "HEAD")
    docs = [a for a in r["added"] if a["channel"] == "docs"]
    assert docs and docs[0]["supersedes"] is None
    from memway.review import render
    assert "first entry in this channel" in render(r)


def test_review_reports_rewritten_history_rather_than_diffing_it(repo):
    """Append-only is the assumption the whole staleness model rests on.

    If old entries are not a prefix of new ones, somebody edited or
    deleted history. Silently diffing that would present a rewrite as an
    ordinary change.
    """
    f = next((repo / ".coord" / "meta").glob("*/notes.jsonl"))
    rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    rows[0]["text"] = "quietly altered"
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    from memway.review import review, render
    r = review(str(repo), "HEAD")
    assert r["rewritten"], "a rewritten history was reported as normal"
    assert "REWRITTEN HISTORY" in render(r)


def test_the_gate_blocks_on_authored_knowledge(repo):
    """Nonzero exit when a change staled a reason somebody wrote down."""
    assert _cli("verify-change", repo, "--gate").returncode == 0

    (repo / "m.py").write_text(
        'def alpha(x):\n    """Doc."""\n    return x * 99\n')
    r = _cli("verify-change", repo, "--gate")
    assert r.returncode == 1, "the gate did not block on a staled note"
    assert "GATE:" in r.stdout and "[notes]" in r.stdout, r.stdout


def test_the_gate_does_not_block_on_confirms(repo):
    """THE SCOPING, and it is the whole design.

    One change to parsers.py in this repo staled ELEVEN coordinates at
    once. A contributor facing eleven blocking items they cannot judge
    clicks confirm to clear them - which manufactures the confirm fatigue
    the gate exists to prevent. This project already has the specimen:
    seven identical confirms on memway.__init__ across seven releases.

    So a stale confirm is reported and never blocks.
    """
    _cli("meta", repo, "alpha", "confirm", "Read it, still accurate.")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "confirmed", "--no-gpg-sign")

    (repo / "m.py").write_text(
        'def alpha(x):\n    """Doc."""\n    return x * 99\n')
    # answer the NOTE, leave the confirm stale
    _cli("meta", repo, "alpha", "notes", "SUPERSEDES: it is *99 now.")

    from memway.query import verify_change
    staled = verify_change(str(repo)).get("staled_knowledge") or []
    assert any(e["channel"] == "confirm" for e in staled), (
        "fixture failed: no stale confirm remains, so this asserts nothing")

    r = _cli("verify-change", repo, "--gate")
    assert r.returncode == 0, (
        f"a stale confirm blocked the gate:\n{r.stdout[-400:]}")


def test_the_channel_is_the_path_so_codeowners_needs_no_tooling(repo):
    """Review policy per channel is expressible in plain CODEOWNERS
    because the layout already put the channel in the filename."""
    files = {f.name for f in (repo / ".coord" / "meta").glob("*/*.jsonl")}
    assert "notes.jsonl" in files, files
    owners = (HERE / ".github" / "CODEOWNERS").read_text()
    assert ".coord/meta/**/confirm.jsonl" in owners
    assert ".coord/meta/**/notes.jsonl" in owners
    # PATTERNS, not prose: the file explains why the derived index is
    # excluded, so a substring check matches its own comment.
    patterns = [l.split()[0] for l in owners.splitlines()
                if l.strip() and not l.lstrip().startswith("#")]
    assert not any(p.startswith(".coord/index") for p in patterns), (
        f"the derived index must not require human review: {patterns}")


def test_knowledge_is_bounded_and_the_deciding_entry_survives(repo):
    """Accumulation costs the payload, and history is what should give.

    Knowledge is append-only and never deleted - authored content is
    precious - so a well-used coordinate grows. before_edit on this
    repo's own before_edit shipped 12 entries, 10,587 of a 14,673
    character payload, and ELEVEN of the twelve were SUPERSEDED: history,
    not warnings.

    metadata.for_display already draws that line - superseded means
    somebody answered it - so superseded entries are exactly the right
    thing to truncate. Nothing is lost; it stays on disk.

    THE ENTRY THAT DECIDES IS NEVER CUT. That is the whole property: a
    bounded list that dropped the unsuperseded entry would hide the
    warning and keep the history, which is worse than not bounding.
    """
    for i in range(10):
        _cli("meta", repo, "alpha", "notes", f"observation number {i}")
    _cli("meta", repo, "alpha", "confirm", "Read it, still accurate.")

    from memway.query import before_edit, show
    from memway.payload import CAP
    for surface in (before_edit(str(repo), "alpha"), show(str(repo), "alpha")):
        total, shown = surface["knowledge_total"], surface["knowledge_shown"]
        assert total > shown, f"nothing was bounded: {shown} of {total}"
        assert shown == len(surface["knowledge"]) <= CAP
        live = [k for k in surface["knowledge"] if not k["superseded"]]
        assert live, (
            "every entry shown is superseded history - the entry that "
            "decides was truncated away")
        assert surface["knowledge"][0]["superseded"] is False, (
            "history is ordered ahead of the deciding entry")
