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


# ---------------------------------------------------------------- unparsed

def test_a_file_nobody_parsed_is_not_a_file_with_no_changes(tmp_path):
    """THE HOLE, and the gate made it worse before this.

    PythonParser returned ([], []) on a SyntaxError, so a broken file
    indexed as zero entities - indistinguishable from an empty file.
    verify-change then reported "no entity-level changes detected" and
    --gate exited 0: CI would certify a change whose impact had never
    been computed. A gate that passes on an unanalysed change converts
    "we did not look" into "we checked", which is worse than no gate.
    """
    (tmp_path / "ok.py").write_text("def good(x):\n    return x + 1\n")
    assert _cli("init", tmp_path).returncode == 0
    (tmp_path / "bad.py").write_text("def broken(:\n    nope\n")

    from memway.query import verify_change
    r = verify_change(str(tmp_path))
    assert r.get("unparsed"), "an unparsed file was reported as nothing"
    assert "bad.py" in r["unparsed"][0]["file"]
    assert "UNKNOWN" in r["note"], r["note"]

    g = _cli("verify-change", tmp_path, "--gate")
    assert g.returncode == 1, "the gate passed on a file nobody parsed"
    assert "could not be parsed" in g.stdout


# ------------------------------------------------------------------ search

def test_search_finds_prior_reasoning_by_subject(repo):
    """The one read that does NOT start from a coordinate.

    Every other surface needs you to already know where to look, which
    means accumulated knowledge is stored but not findable and the same
    ground gets re-derived.
    """
    _cli("meta", repo, "alpha", "notes",
         "Proxy handling: we pass the env var through untouched.")
    from memway.review import search
    r = search(str(repo), "proxy")
    assert r["hits_total"] >= 1, r
    assert any("alpha" in h["qualname"] for h in r["hits"])
    assert "proxy" in r["hits"][0]["entries"][0]["excerpt"].lower()
    assert search(str(repo), "nothing-mentions-this")["hits_total"] == 0
    assert search(str(repo), "")["error"]
    assert search(str(repo), "proxy", channel="nope")["error"]


def test_search_returns_superseded_entries_marked_as_history(repo):
    """What somebody already considered and replaced is often exactly
    what you want when asking whether a thing was thought about - but it
    has to arrive labelled, or it reads as current belief."""
    _cli("meta", repo, "alpha", "notes", "Retry policy: three attempts.")
    _cli("meta", repo, "alpha", "notes", "SUPERSEDES: retry policy is now one.")
    from memway.review import search
    r = search(str(repo), "retry policy")
    entries = r["hits"][0]["entries"]
    assert any(e["superseded"] for e in entries), (
        "superseded reasoning was dropped; it is the part that answers "
        "'was this considered before'")
    assert any(not e["superseded"] for e in entries)


# ----------------------------------------------------------------- markers

def test_one_marker_belongs_to_one_entity(tmp_path):
    """A FIXME inside a method belonged to the method AND its class AND
    its module, so the queue listed it three times - three comments
    produced six markers, the attention queue inflating its own count.

    The comment's `line` is ENTITY-RELATIVE, which is why keying on it
    directly deduped nothing: the same FIXME reads line 5 on the module,
    4 on the class and 2 on the method. lineno + line - 1 makes them one.
    """
    (tmp_path / "m.py").write_text(
        "# XXX: this needs work\n"
        "class Thing:\n"
        "    # TODO: and this\n"
        "    def go(self):\n"
        "        # FIXME: really\n"
        "        return 1\n")
    _cli("init", tmp_path)
    from memway.query import attention
    a = attention(str(tmp_path))
    assert a["markers_total"] == 3, (
        f"{a['markers_total']} markers for 3 comments: {a['markers']}")
    owner = {m["tag"]: m["entity"] for m in a["markers"]}
    assert owner["FIXME"].endswith("Thing.go"), owner
    assert owner["TODO"].endswith("Thing"), owner
    assert owner["XXX"] == "m", owner


# ------------------------------------------------------------- replaces

def test_supersession_can_carry_its_reason(repo):
    """A pair of texts leaves the reader to infer WHY the belief changed,
    and the why is the part that does not survive in anybody's head."""
    _cli("meta", repo, "alpha", "notes", "It is *99 now.",
         "--replaces", "callers moved to 0-indexing in #412")
    from memway.review import review, render
    r = review(str(repo), "HEAD")
    a = [x for x in r["added"] if "99" in x["text"]][0]
    assert a["replaces"] == "callers moved to 0-indexing in #412"
    assert "because: callers moved to 0-indexing" in render(r)


# ------------------------------------------------------- knowledge at risk

def test_notes_on_callers_that_name_the_change_are_flagged(tmp_path):
    """One layer out from staled_knowledge, and no stamp will ever catch it.

    A note on a CALLER that names the thing you changed stays FRESH - the
    caller's own body did not move - while the fact it describes has
    changed underneath.

    A HEURISTIC, LABELLED. Reported separately, never counted with
    staled_knowledge, and --gate does not block on it: a mention is where
    to look, not proof of error.
    """
    (tmp_path / "m.py").write_text(
        "def fetch(url, timeout):\n    return url\n\n"
        "def caller(u):\n    return fetch(u, 5)\n")
    _cli("init", tmp_path)
    _cli("meta", tmp_path, "caller", "notes",
         "Relies on fetch defaulting to a 5s timeout.")
    (tmp_path / "m.py").write_text(
        "def fetch(url, timeout, retries):\n    return url\n\n"
        "def caller(u):\n    return fetch(u, 5)\n")

    from memway.query import verify_change
    r = verify_change(str(tmp_path))
    risk = r["knowledge_at_risk"]
    assert risk, "a caller's note naming the changed entity was not flagged"
    assert any(m.endswith("fetch") for m in risk[0]["mentions"]), risk
    assert not any(k["coordinate"] == risk[0]["coordinate"]
                   for k in (r.get("staled_knowledge") or [])), (
        "an at-risk note was counted as staled - a guess promoted to a verdict")
    assert _cli("verify-change", tmp_path, "--gate").returncode == 0, (
        "the gate blocked on a heuristic")


# --------------------------------------------------------------- typescript

def test_typescript_renames_carry_their_knowledge(tmp_path):
    """`parseable` and `supported` are different claims.

    The TS grammar was wired and rename tracking was never proven, so the
    honest status was "we think so". This is the proof: rename a method,
    the lineage records it and the note follows.
    """
    pytest.importorskip("tree_sitter_typescript")
    (tmp_path / "svc.ts").write_text(
        "export class Client {\n"
        "  fetchUser(id: string): string {\n    return id;\n  }\n}\n")
    _cli("init", tmp_path)
    _cli("meta", tmp_path, "fetchUser", "notes",
         "Returns the raw id; callers must not assume a User object.")
    (tmp_path / "svc.ts").write_text(
        "export class Client {\n"
        "  loadUser(id: string): string {\n    return id;\n  }\n}\n")
    _cli("index", tmp_path)

    from memway.query import lineage, show
    lin = lineage(str(tmp_path), "loadUser")
    assert lin.get("history"), f"no rename recorded: {lin}"
    assert "fetchUser" in lin["history"][0]["note"], lin
    texts = [k["text"] for k in show(str(tmp_path), "loadUser")["knowledge"]]
    assert any("raw id" in t for t in texts), (
        f"the note did not follow the rename: {texts}")
