"""memway_dig: mechanics only, and the fence around it.

The load-bearing claim is NEGATIVE: this tool never gates, never scores,
and never writes to .coord. Judgment belongs to the session agent. The
fence is tested directly (byte-identical .coord across a dig), because a
tool that quietly grew a scoring pass would still look correct from the
outside for a long time.

Everything else here is mechanics that cost something to learn:
line-range -L (the funcname form is .gitattributes-dependent and fails on
real repos), provenance labelling (a range outlives the entity in it),
the forge hop (on forge-centric repos the only explanation is in the PR),
and tag reconciliation (a main sha reports "unreleased" for a change that
shipped under a backport).
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import dig as digmod
from memway.dig import (dig, BACKPORT_WARNING, REGION_HISTORY, ENTITY_HISTORY,
                        FORGE_NO_GH, FORGE_UNAUTH, FORGE_NOT_GITHUB,
                        FORGE_FETCH_FAILED, MCP_CAP_BYTES)


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "memway.cli", *[str(a) for a in args]],
        capture_output=True, text=True, cwd=str(HERE))


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def commit(repo, message):
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=T",
        "commit", "-m", message, "--no-gpg-sign")


@pytest.fixture
def two_era(tmp_path):
    """A file whose LINES outlive the entity occupying them.

    alpha lives at lines 1-2 for two commits, is replaced by beta at the
    same lines, and beta is then edited. A dig on beta must walk back
    through alpha's commits (that is what -L does) and must LABEL them,
    because they are the region's history and not this entity's.
    """
    R = tmp_path / "era"
    R.mkdir()
    git(R, "init", "-q", "-b", "main")
    (R / "mod.py").write_text("def alpha(x):\n    return x\n")
    commit(R, "c1: add alpha\n\nBecause the caller needed identity.")
    (R / "mod.py").write_text("def alpha(x):\n    return x + 1\n")
    commit(R, "c2: alpha adds one (#11)\n\nOff-by-one at the call site.")
    (R / "mod.py").write_text("def beta(x):\n    return x * 2\n")
    commit(R, "c3: replace alpha with beta\n\nDoubling is the new contract.")
    (R / "mod.py").write_text("def beta(x):\n    return x * 3\n")
    commit(R, "c4: beta triples (#22)\n\nPricing moved to thirds.")
    # gamma is APPENDED onto lines that never held anything else, so its
    # range has no prior era. It is the control for the labelling test:
    # without it, "everything is region-history" would also pass.
    (R / "mod.py").write_text("def beta(x):\n    return x * 3\n\n"
                              "def gamma(y):\n    return y\n")
    commit(R, "c5: add gamma\n\nCallers needed a passthrough.")
    r = cli("init", R)
    assert r.returncode == 0, r.stderr[-400:]
    return R


def coord_fingerprint(repo: Path) -> dict:
    """Every byte under .coord, addressed by path."""
    out = {}
    for p in sorted((repo / ".coord").rglob("*")):
        if p.is_file():
            out[str(p.relative_to(repo))] = hashlib.sha256(
                p.read_bytes()).hexdigest()
    return out


# ------------------------------------------------------------- the fence

def test_the_fence_dig_never_writes_to_coord(two_era):
    """THE FENCE. A dig is a read. If this ever fails, the tool has grown
    a side effect and the contract is void - do not 'fix' the test."""
    before = coord_fingerprint(two_era)
    assert before, "fixture must have a map to begin with"
    out = dig(str(two_era), "mod.beta")
    assert out["candidates"], out
    after = coord_fingerprint(two_era)
    assert before == after, "dig mutated .coord"
    assert set(before) == set(after), "dig added or removed a file in .coord"


def test_the_fence_holds_on_the_mcp_path_too(two_era):
    before = coord_fingerprint(two_era)
    from memway.mcp import _dig_capped
    _dig_capped(str(two_era), "mod.beta")
    assert coord_fingerprint(two_era) == before


def test_the_fence_holds_through_the_cli(two_era):
    before = coord_fingerprint(two_era)
    r = cli("dig", two_era, "mod.beta")
    assert r.returncode == 0, r.stderr[-400:]
    assert coord_fingerprint(two_era) == before


def test_full_jsonrpc_call_writes_only_the_usage_log(two_era):
    """The fence's exact boundary, pinned rather than glossed.

    dig() itself writes nothing. But a real MCP `tools/call` goes through
    the server, and the server logs EVERY tool invocation to
    .coord/log/usage.jsonl - pre-existing behaviour shared by all ten
    tools, and the reason `.coord/log/` is gitignored as personal-machine
    data rather than map content.

    So the honest claim is: the TOOL writes nothing; the TRANSPORT keeps
    its own telemetry. This test fails if a dig ever touches anything
    else - index, meta, cache, versions.
    """
    from memway.mcp import handle
    before = coord_fingerprint(two_era)
    resp = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "memway_dig",
                              "arguments": {"ref": "mod.beta"}}},
                  str(two_era))
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["entity"]["qualname"] == "mod.beta"
    assert not resp["result"].get("isError")
    after = coord_fingerprint(two_era)
    changed = {k for k in set(before) | set(after)
               if before.get(k) != after.get(k)}
    assert changed == {".coord/log/usage.jsonl"}, \
        f"a dig touched more than the usage log: {changed}"


def test_payload_states_the_contract(two_era):
    """The fence must be legible to the consumer, not just enforced."""
    out = dig(str(two_era), "mod.beta")
    c = out["contract"].lower()
    assert "candidates only" in c
    assert "caller" in c
    assert "never gates" in c and "scores" in c and "writes" in c


def test_no_scoring_or_gating_fields_are_emitted(two_era):
    """Negative surface check: a rank/score/verdict field appearing here
    would mean the fence moved."""
    out = dig(str(two_era), "mod.beta")
    banned = {"rank", "score", "verdict", "rationale", "is_rationale",
              "keep", "qualifies", "signals"}
    for c in out["candidates"]:
        assert not (banned & set(c)), f"scoring field leaked: {banned & set(c)}"
    # and every commit the range touched is returned, none filtered away
    n = len(subprocess.run(
        ["git", "-C", str(two_era), "log", "--format=%H", "-L1,2:mod.py", "-s"],
        capture_output=True, text=True).stdout.split())
    assert out["counts"]["total"] == n, "a commit was gated out"


# ------------------------------------------------- D-D2: the -L invocation

def test_uses_line_range_form_never_funcname(two_era, monkeypatch):
    """The funcname form is .gitattributes-dependent and fails outright on
    real repos (measured: `fatal: -L parameter '_order_by_pairs'` on
    Django). The range comes from the map instead."""
    seen = []
    real = digmod.subprocess.run

    def spy(argv, **kw):
        seen.append(argv)
        return real(argv, **kw)

    monkeypatch.setattr(digmod.subprocess, "run", spy)
    out = dig(str(two_era), "mod.beta")
    logs = [a for a in seen if "log" in a and any(
        str(x).startswith("-L") for x in a)]
    assert logs, "no -L invocation was made"
    for argv in logs:
        flag = next(x for x in argv if str(x).startswith("-L"))
        assert flag.startswith("-L1,2:") or "," in flag, \
            f"not the line-range form: {flag}"
        assert ":mod.py" in flag
        assert not flag.startswith("-L:"), "funcname form is forbidden"
        assert "beta" not in flag, "range form must not name the function"
    assert out["dig"]["command"].startswith("git log -L1,2:")
    assert "line-range" in out["dig"]["form"]


def test_range_is_taken_from_the_map_not_recomputed(two_era):
    from memway.indexer import Indexer
    ix = Indexer(two_era, two_era / ".coord")
    ix.load_existing()
    e = ix.resolve("mod.beta")
    out = dig(str(two_era), "mod.beta")
    assert out["entity"]["lineno"] == e.lineno
    assert out["entity"]["end_lineno"] == e.end_lineno
    assert out["entity"]["coord_id"] == e.coord_id
    assert out["entity"]["path"] == e.path


def test_boundary_search_uses_follow(two_era, monkeypatch):
    """Without --follow the boundary search stops at the last file RENAME
    instead of the entity's creation - measured on memway's own map, where
    the coordsys->memway rename hid it and every candidate came back
    mislabelled as entity-history."""
    seen = []
    real = digmod.subprocess.run
    monkeypatch.setattr(digmod.subprocess, "run",
                        lambda a, **k: (seen.append(a), real(a, **k))[1])
    dig(str(two_era), "mod.beta")
    sflags = [a for a in seen if any(str(x).startswith("-S") for x in a)]
    assert sflags, "no -S boundary search was made"
    assert any("--follow" in a for a in sflags), "boundary search lost --follow"


# --------------------------------------------- D-D3: provenance labelling

def test_pre_extraction_commits_are_labelled(two_era):
    """The range outlives the entity. alpha's commits are the region's
    history, not beta's, and saying so is the tool's job."""
    out = dig(str(two_era), "mod.beta")
    by_sub = {c["subject"].split(":")[0]: c for c in out["candidates"]}
    assert set(by_sub) == {"c1", "c2", "c3", "c4"}, by_sub.keys()
    assert by_sub["c4"]["provenance"] == ENTITY_HISTORY
    assert by_sub["c3"]["provenance"] == ENTITY_HISTORY, \
        "the creation commit is the entity's own history, not the region's"
    assert by_sub["c2"]["provenance"] == REGION_HISTORY
    assert by_sub["c1"]["provenance"] == REGION_HISTORY
    assert out["counts"]["region_history"] == 2
    assert out["counts"]["entity_history"] == 2
    assert out["counts"]["total"] == 4


def test_creation_boundary_is_reported(two_era):
    out = dig(str(two_era), "mod.beta")
    b = out["dig"]["creation_boundary"]
    assert b, "boundary must be found in this fixture"
    c3 = next(c for c in out["candidates"] if c["subject"].startswith("c3"))
    assert b == c3["sha"], "boundary must be the commit that introduced beta"


def test_entity_with_no_prior_region_has_no_region_history(two_era):
    """gamma was appended onto fresh lines, so nothing precedes it.

    The control for the labelling rule: without a case that must come
    back with ZERO region-history, a bug that labels everything
    pre-extraction would still pass the test above.
    """
    out = dig(str(two_era), "mod.gamma")
    assert "error" not in out, out
    assert out["counts"]["total"] >= 1
    assert out["counts"]["region_history"] == 0, \
        [c["subject"] for c in out["candidates"]
         if c["provenance"] == REGION_HISTORY]
    assert all(c["provenance"] == ENTITY_HISTORY for c in out["candidates"])


def test_entity_older_than_the_range_history_is_proven_not_guessed(
        two_era, monkeypatch):
    """Measured on matplotlib: get_width_height was introduced 2005-06-18
    while its range's own -L history starts 2005-07-22. The creation
    commit is outside the candidate set, but it is an ANCESTOR of the
    oldest candidate - so 'all entity-history' is provable. Reporting
    'unverified' there would overclaim uncertainty."""
    from memway.dig import PREDATES_RANGE
    monkeypatch.setattr(digmod, "_creation_boundary",
                        lambda *a, **k: PREDATES_RANGE)
    out = dig(str(two_era), "mod.beta")
    assert out["dig"]["creation_boundary"] == PREDATES_RANGE
    assert out["counts"]["region_history"] == 0
    assert all(c["provenance"] == ENTITY_HISTORY for c in out["candidates"])
    assert any("proven by ancestry" in n for n in out["notes"])
    assert not any("unverified" in n for n in out["notes"]), \
        "must not claim uncertainty it does not have"


def test_missing_boundary_degrades_honestly(two_era, monkeypatch):
    """Unknown provenance must be stated, not assumed."""
    monkeypatch.setattr(digmod, "_creation_boundary", lambda *a, **k: None)
    out = dig(str(two_era), "mod.beta")
    assert all(c["provenance"] == ENTITY_HISTORY for c in out["candidates"])
    assert any("boundary not found" in n for n in out["notes"])
    assert any("unverified" in n for n in out["notes"]), \
        "must not claim proven provenance it does not have"


# ---------------------------------------------------- D-D4: the forge leg

class FakeProc:
    def __init__(self, rc=0, stdout=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, ""


def _fake_forge(monkeypatch, *, which="gh", auth_rc=0, remote=None,
                pr_rc=0, pr_body="PR BODY"):
    """Mock only the forge calls; git log/tag stay real."""
    real = digmod.subprocess.run
    monkeypatch.setattr(digmod.shutil, "which", lambda n: which)

    def fake(argv, **kw):
        if argv and argv[0] == "gh":
            if argv[1:3] == ["auth", "status"]:
                return FakeProc(auth_rc)
            if argv[1:3] == ["pr", "view"]:
                return FakeProc(pr_rc, json.dumps({"body": pr_body}))
        if remote is not None and "remote" in argv:
            return FakeProc(0, remote + "\n")
        return real(argv, **kw)

    monkeypatch.setattr(digmod.subprocess, "run", fake)


def test_forge_refs_extracted_and_fetched(two_era, monkeypatch):
    _fake_forge(monkeypatch, remote="https://github.com/o/r.git",
                pr_body="Because the pricing table stores thirds.")
    out = dig(str(two_era), "mod.beta")
    refs = {r["number"]: r for c in out["candidates"] for r in c["pr_refs"]}
    assert 22 in refs, "#22 from c4's subject trailer must be extracted"
    assert 11 in refs, "#11 from c2's subject trailer must be extracted"
    assert refs[22]["body"] == "Because the pricing table stores thirds."
    assert refs[22]["unavailable_reason"] is None


def test_forge_refs_deduplicated_per_commit(monkeypatch, two_era):
    _fake_forge(monkeypatch, remote="https://github.com/o/r.git")
    monkeypatch.setattr(digmod, "_log_range", lambda *a, **k: [
        {"sha": "a" * 40, "short_sha": "aaaaaaa", "date": "2026-01-01",
         "author": "T", "subject": "fix #7 and #7 again",
         "body": "see #7 and #7"}])
    out = dig(str(two_era), "mod.beta")
    nums = [r["number"] for r in out["candidates"][0]["pr_refs"]]
    assert nums == [7], f"duplicate refs not collapsed: {nums}"


@pytest.mark.parametrize("kw,reason", [
    (dict(which=None), FORGE_NO_GH),
    (dict(auth_rc=1), FORGE_UNAUTH),
    (dict(remote="git@gitlab.com:o/r.git"), FORGE_NOT_GITHUB),
    (dict(pr_rc=1), FORGE_FETCH_FAILED),
])
def test_forge_degrades_gracefully_with_a_reason(two_era, monkeypatch,
                                                 kw, reason):
    """Every forge failure mode returns candidates anyway, and NAMES why.
    The dig must never fail on the forge leg."""
    kw.setdefault("remote", "https://github.com/o/r.git")
    _fake_forge(monkeypatch, **kw)
    out = dig(str(two_era), "mod.beta")
    assert out["candidates"], "forge failure must not empty the dig"
    assert out["counts"]["total"] == 4, "forge failure must not drop commits"
    refs = [r for c in out["candidates"] for r in c["pr_refs"]]
    assert refs, "refs must still be listed, just unresolved"
    assert all(r["body"] is None for r in refs)
    assert {r["unavailable_reason"] for r in refs} == {reason}


def test_trac_style_refs_are_reported_but_never_fetched(two_era, monkeypatch):
    """MEASURED on the Django answer key: bare '#NNNN' in a commit message
    is a TRAC ticket there, and django/django on GitHub has PRs in the same
    numeric range. Fetching by bare number attached four unrelated PR
    bodies - 'Fixed #1142 -- multiple database support' got PR #1142 about
    Urdu RTL locales. Confidently wrong is worse than unavailable.
    """
    fetched = []
    _fake_forge(monkeypatch, remote="https://github.com/o/r.git")
    real = digmod._fetch_pr
    monkeypatch.setattr(digmod, "_fetch_pr",
                        lambda s, n, c: (fetched.append(n), real(s, n, c))[1])
    monkeypatch.setattr(digmod, "_log_range", lambda *a, **k: [
        {"sha": "a" * 40, "short_sha": "aaaaaaa", "date": "2026-01-01",
         "author": "T", "subject": "Fixed #1142 -- Added multiple db support",
         "body": "Refs #14357 and see #20413"}])
    out = dig(str(two_era), "mod.beta")
    refs = out["candidates"][0]["pr_refs"]
    assert {r["number"] for r in refs} == {1142, 14357, 20413}, \
        "the numbers must still be REPORTED so the caller can look them up"
    assert all(r["body"] is None for r in refs)
    assert all(r["unavailable_reason"] == digmod.AMBIGUOUS_REF for r in refs)
    assert fetched == [], f"no bare-number ref may be fetched: {fetched}"


@pytest.mark.parametrize("subject,body,expect_fetch", [
    ("ci: make eslint fail (#32183)", "", True),        # squash trailer
    ("Backport PR #32038: fix canvas", "", True),       # explicit PR
    ("Merge pull request #77 from x/y", "", True),      # merge commit
    ("Fixed #36795 -- Enforced quoting", "", False),    # Trac prose
    ("Refs #14357 -- Deprecated Meta", "", False),      # Trac prose
    ("fix crash", "closes #91", False),                 # bare body ref
])
def test_only_github_shaped_refs_are_resolved(two_era, monkeypatch,
                                              subject, body, expect_fetch):
    fetched = []
    _fake_forge(monkeypatch, remote="https://github.com/o/r.git")
    real = digmod._fetch_pr
    monkeypatch.setattr(digmod, "_fetch_pr",
                        lambda s, n, c: (fetched.append(n), real(s, n, c))[1])
    monkeypatch.setattr(digmod, "_log_range", lambda *a, **k: [
        {"sha": "a" * 40, "short_sha": "aaaaaaa", "date": "2026-01-01",
         "author": "T", "subject": subject, "body": body}])
    out = dig(str(two_era), "mod.beta")
    refs = out["candidates"][0]["pr_refs"]
    assert refs, "the number must be reported either way"
    assert bool(fetched) is expect_fetch, \
        f"{subject!r}: fetched={fetched}, expected fetch={expect_fetch}"
    if not expect_fetch:
        assert refs[0]["unavailable_reason"] == digmod.AMBIGUOUS_REF


def test_forge_leg_can_be_skipped_entirely(two_era):
    out = dig(str(two_era), "mod.beta", forge=False)
    assert out["candidates"]
    refs = [r for c in out["candidates"] for r in c["pr_refs"]]
    assert all(r["unavailable_reason"] == "forge-leg-disabled" for r in refs)


# ------------------------------------------- D-D5: release reconciliation

def test_untagged_commit_carries_the_backport_warning(two_era):
    """THE WARNING THAT PREVENTS THE MATPLOTLIB WRONG-REPORT. A commit in
    no tag may still have shipped under a backported sha."""
    out = dig(str(two_era), "mod.beta")
    for c in out["candidates"]:
        assert c["released_in"] == [], "fixture has no tags"
        assert BACKPORT_WARNING in c["warnings"]
        assert "backported sha" in BACKPORT_WARNING
        assert "verify on release branches" in BACKPORT_WARNING


def test_tagged_commit_carries_no_backport_warning(two_era):
    git(two_era, "tag", "v1.0")
    out = dig(str(two_era), "mod.beta")
    tagged = [c for c in out["candidates"] if c["released_in"]]
    assert tagged, "tagging HEAD must show up in released_in"
    for c in tagged:
        assert "v1.0" in c["released_in"]
        assert BACKPORT_WARNING not in c["warnings"], \
            "a released commit must not be warned about"


# ------------------------------------------------------- payload byte cap

def _fat(n=40, body_len=4000):
    return [{"sha": f"{i:040x}", "short_sha": f"{i:07x}", "date": "2026-01-01",
             "author": "T", "subject": f"commit {i}", "body": "b" * body_len,
             "provenance": ENTITY_HISTORY, "warnings": [],
             "pr_refs": [{"number": i, "body": "p" * body_len,
                          "unavailable_reason": None}]}
            for i in range(n)]


def test_mcp_path_is_capped_and_marks_every_cut(two_era, monkeypatch):
    """Finding #41: PR bodies are large and a dig can carry dozens."""
    monkeypatch.setattr(digmod, "_log_range", lambda *a, **k: _fat())
    monkeypatch.setattr(digmod, "_creation_boundary", lambda *a, **k: None)
    monkeypatch.setattr(digmod, "_released_in", lambda r, c: [
        x.setdefault("released_in", []) for x in c])
    monkeypatch.setattr(digmod, "_forge_refs", lambda c, r, f: None)
    from memway.mcp import _dig_capped
    out = _dig_capped(str(two_era), "mod.beta")
    size = len(json.dumps(out).encode())
    assert size <= MCP_CAP_BYTES, f"cap breached: {size} > {MCP_CAP_BYTES}"
    truncated = [c for c in out["candidates"] if c.get("truncated")]
    assert truncated, "truncation must be marked per candidate"
    assert any("truncated by memway_dig payload cap" in c["body"]
               for c in truncated)
    if len(out["candidates"]) < 40:
        assert "payload_capped" in out, "dropping candidates must be declared"
        assert out["payload_capped"]["candidates_returned"] == \
            len(out["candidates"])
        assert "uncapped" in out["payload_capped"]["note"]


def test_cli_json_path_is_uncapped(two_era, monkeypatch):
    """A file on disk has no context window."""
    monkeypatch.setattr(digmod, "_log_range", lambda *a, **k: _fat())
    monkeypatch.setattr(digmod, "_creation_boundary", lambda *a, **k: None)
    monkeypatch.setattr(digmod, "_released_in", lambda r, c: [
        x.setdefault("released_in", []) for x in c])
    monkeypatch.setattr(digmod, "_forge_refs", lambda c, r, f: None)
    out = dig(str(two_era), "mod.beta")            # no cap_bytes
    assert len(json.dumps(out).encode()) > MCP_CAP_BYTES
    assert "payload_capped" not in out
    assert len(out["candidates"]) == 40
    assert not any(c.get("truncated") for c in out["candidates"])


def test_cap_prefers_truncating_bodies_over_dropping_commits(two_era,
                                                             monkeypatch):
    """A truncated body still names its commit; a dropped candidate is
    invisible. Order of sacrifice is part of the contract."""
    monkeypatch.setattr(digmod, "_log_range", lambda *a, **k: _fat(6, 3000))
    monkeypatch.setattr(digmod, "_creation_boundary", lambda *a, **k: None)
    monkeypatch.setattr(digmod, "_released_in", lambda r, c: [
        x.setdefault("released_in", []) for x in c])
    monkeypatch.setattr(digmod, "_forge_refs", lambda c, r, f: None)
    out = dig(str(two_era), "mod.beta", cap_bytes=20_000)
    assert len(out["candidates"]) == 6, "no commit should have been dropped"
    assert any(c.get("truncated") for c in out["candidates"])


# ----------------------------------------------------------- surfaces

def test_mcp_registers_dig_and_states_the_callers_job():
    from memway.mcp import TOOLS
    t = next(t for t in TOOLS if t["name"] == "memway_dig")
    d = t["description"].lower()
    assert "candidates" in d
    assert "caller" in d, "the description must hand judgment to the caller"
    assert "never gates" in d and "never writes" in d
    assert t["inputSchema"]["required"] == ["ref"]
    assert len(TOOLS) == 11, "dig ships alongside the other tools"


def test_cli_help_states_the_contract():
    r = cli("--help")
    assert "memway dig" in r.stdout
    assert "CANDIDATES" in r.stdout
    assert "caller's job" in r.stdout


def test_cli_json_query_is_registered(two_era):
    r = cli("--json", "dig", two_era, "mod.beta")
    assert r.returncode == 0, r.stderr[-400:]
    out = json.loads(r.stdout)
    assert out["entity"]["qualname"] == "mod.beta"
    assert out["counts"]["total"] == 4
    assert "contract" in out


@pytest.mark.parametrize("field,typ", [
    ("sha", str), ("short_sha", str), ("date", str), ("author", str),
    ("subject", str), ("body", str), ("provenance", str),
    ("pr_refs", list), ("released_in", list), ("warnings", list),
])
def test_every_candidate_carries_the_full_return_shape(two_era, field, typ):
    """The return shape is the contract with the caller; a missing field
    forces the caller to guess."""
    out = dig(str(two_era), "mod.beta")
    assert out["candidates"]
    for c in out["candidates"]:
        assert field in c, f"{field} missing from {c.get('short_sha')}"
        assert isinstance(c[field], typ), f"{field} is {type(c[field])}"
    if field == "provenance":
        assert all(c[field] in (ENTITY_HISTORY, REGION_HISTORY)
                   for c in out["candidates"])
    if field == "sha":
        assert all(len(c[field]) == 40 for c in out["candidates"])


def test_bodies_are_verbatim_when_uncapped(two_era):
    """No silent trimming on the CLI path - the caller judges the text,
    so the text must be the commit's own."""
    out = dig(str(two_era), "mod.beta")
    c4 = next(c for c in out["candidates"] if c["subject"].startswith("c4"))
    assert c4["body"] == "Pricing moved to thirds."
    assert "truncated" not in c4
    real = subprocess.run(["git", "-C", str(two_era), "log", "-1",
                           "--format=%b", c4["sha"]],
                          capture_output=True, text=True).stdout.strip()
    assert c4["body"] == real, "body must match git verbatim"


def test_untracked_path_degrades_with_a_note(two_era, monkeypatch):
    """A dig that finds nothing must say why, not return a bare empty."""
    monkeypatch.setattr(digmod, "_log_range", lambda *a, **k: [])
    out = dig(str(two_era), "mod.beta")
    assert out["candidates"] == []
    assert out["counts"]["total"] == 0
    assert any("returned nothing" in n for n in out["notes"])
    assert "contract" in out, "the contract holds even for an empty dig"


def test_git_failure_does_not_raise(two_era, monkeypatch):
    """A dead git is a degraded dig, not a crash - the caller is an agent
    mid-task."""
    monkeypatch.setattr(digmod.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    out = dig(str(two_era), "mod.beta")
    assert "candidates" in out
    assert out["candidates"] == []
    assert any("returned nothing" in n for n in out["notes"])


def test_unresolvable_ref_is_actionable(two_era):
    out = dig(str(two_era), "mod.nosuchthing")
    assert "error" in out
    assert "closest" in out and out["closest"]
    assert not any(k in out for k in ("candidates", "counts"))
