"""The evidence layer: derived vs authored, kept apart by construction.

The whole feature is one distinction:

  EVIDENCE is what the record SAYS - regenerable, clearable, derived.
  VERDICT  is what a reader CONCLUDED - irreplaceable, authored.

Every test here defends that line somewhere it could blur: clearing,
truncating, rendering, caching. The two that matter most are negative -
clearing evidence must never reach authored knowledge, and truncation
must sacrifice evidence before it touches a single authored entry.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import evidence as ev
from memway import query
from memway.metadata import MetaStore, CHANNELS
from memway.indexer import Indexer
import memway.dig as digmod


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "memway.cli", *[str(a) for a in args]],
        capture_output=True, text=True, cwd=str(HERE))


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def commit(repo, msg):
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=T", "commit",
        "-m", msg, "--no-gpg-sign")


SRC = '''"""Module m."""


def alpha(x):
    """Alpha."""
    y = x + 1
    return y
'''


@pytest.fixture
def repo(tmp_path):
    R = tmp_path / "proj"
    R.mkdir()
    git(R, "init", "-q", "-b", "main")
    (R / "m.py").write_text(SRC)
    commit(R, "seed")
    (R / "m.py").write_text(SRC.replace("y = x + 1", "y = x + 2"))
    commit(R, "m: bump the increment (#11)\n\nCallers depended on the old "
              "value, so this needed a migration note.")
    assert cli("init", R).returncode == 0
    return R


def coord_of(R, ref="m.alpha"):
    ix = Indexer(R, R / ".coord")
    ix.load_existing()
    return ix.resolve(ref)


def meta_fingerprint(R):
    """Every authored byte. Evidence lives outside this by construction."""
    meta = R / ".coord" / "meta"
    return {str(p.relative_to(R)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(meta.rglob("*")) if p.is_file()}


# ------------------------------------------------- derived vs authored

@pytest.mark.parametrize("channel", CHANNELS)
def test_clear_evidence_never_touches_any_authored_channel(repo, channel):
    """The load-bearing negative. Parametrized across EVERY channel so a
    new one cannot quietly fall outside the guarantee."""
    e = coord_of(repo)
    store = MetaStore(repo / ".coord")
    store.add(e.coord_id, channel, f"authored {channel} entry",
              author="test", body_hash=e.body_hash)
    digmod.dig(str(repo), "m.alpha", cache=True)
    assert ev.read(repo / ".coord", e.coord_id), "evidence must exist first"
    before = meta_fingerprint(repo)
    assert before

    r = ev.clear(repo / ".coord")

    assert r["cleared"] > 0
    assert not ev.evidence_root(repo / ".coord").exists()
    assert meta_fingerprint(repo) == before, \
        f"clearing evidence altered authored {channel}"
    got = store.read_all(e.coord_id,
                         current_hash={e.logic_hash, e.body_hash})
    assert any(x["text"] == f"authored {channel} entry"
               for xs in got.values() for x in xs)


def test_evidence_is_a_sibling_of_meta_not_a_child(repo):
    """Layout IS the guarantee: clear() cannot reach meta because it
    never addresses it."""
    root = ev.evidence_root(repo / ".coord")
    meta = repo / ".coord" / "meta"
    assert root.parent == meta.parent
    assert meta not in root.parents and root not in meta.parents


def test_evidence_is_registered_as_derived_in_gitignore():
    ignored = (HERE / ".gitignore").read_text()
    assert ".coord/evidence/" in ignored, \
        "evidence is regenerable and must not enter the authored diff"
    patterns = [l.strip() for l in ignored.splitlines()
                if l.strip() and not l.strip().startswith("#")]
    assert not any(p.startswith(".coord/meta") and "excavated" not in p
                   for p in patterns), "authored meta stays tracked"


# ------------------------------------------------------------- caching

def test_second_dig_walks_no_history(repo):
    """A cache hit must cost nothing but the staleness check itself."""
    calls = []
    real = digmod.subprocess.run
    digmod.subprocess.run = lambda a, **k: (
        calls.append(" ".join(map(str, a))), real(a, **k))[1]
    try:
        first = digmod.dig(str(repo), "m.alpha", cache=True)
        calls.clear()
        second = digmod.dig(str(repo), "m.alpha", cache=True)
    finally:
        digmod.subprocess.run = real
    history = [c for c in calls if " log " in c or "tag" in c or c.startswith("gh")]
    assert history == [], f"a cache hit walked history: {history}"
    assert len(calls) == 1 and "rev-parse HEAD" in calls[0], \
        "exactly one call, and only to check whether the cache is stale"
    assert second["evidence"]["cache_hit"] is True
    strip = lambda p: [{k: v for k, v in c.items() if k != "warnings"}
                       for c in p["candidates"]]
    assert strip(second) == strip(first), "cached output must not thin out"


def test_cached_dig_preserves_pr_bodies(repo, monkeypatch):
    """Bodies are the bulk and the point; a cache that drops them looks
    identical until someone needs one."""
    monkeypatch.setattr(digmod, "_gh_ready", lambda r: ("o/r", None))
    monkeypatch.setattr(digmod, "_fetch_pr",
                        lambda s, n, c: ("PR BODY " + n, None))
    first = digmod.dig(str(repo), "m.alpha", cache=True)
    live = [r for c in first["candidates"] for r in c["pr_refs"] if r["body"]]
    assert live, "fixture must produce at least one PR ref"
    second = digmod.dig(str(repo), "m.alpha", cache=True)
    cached = [r for c in second["candidates"] for r in c["pr_refs"] if r["body"]]
    assert {r["number"] for r in cached} == {r["number"] for r in live}
    assert {r["body"] for r in cached} == {r["body"] for r in live}


def test_grown_history_is_reported_and_refetched(repo):
    """Two-axis staleness: evidence goes stale when HISTORY grows, which
    is independent of whether the code changed."""
    digmod.dig(str(repo), "m.alpha", cache=True)
    before = ev.dug_through(repo / ".coord", coord_of(repo).coord_id)
    assert before
    (repo / "m.py").write_text(SRC.replace("y = x + 1", "y = x + 3"))
    commit(repo, "m: third increment\n\nAnother reason entirely.")
    out = digmod.dig(str(repo), "m.alpha", cache=True)
    assert out["evidence"].get("cache_hit") is not True, "history moved"
    assert any("was current through" in n for n in out["notes"])
    after = ev.dug_through(repo / ".coord", coord_of(repo).coord_id)
    assert after != before, "the marker must advance"


def test_dig_without_cache_writes_no_evidence(repo):
    digmod.dig(str(repo), "m.alpha")
    assert not ev.evidence_root(repo / ".coord").exists(), \
        "caching is opt-in; a plain dig stays a pure read"


# --------------------------------------------------------- the read fence

@pytest.mark.parametrize("fn", ["before_edit", "show"])
def test_read_surfaces_never_write_evidence(repo, fn):
    """A briefing that populated the cache would make every read a write."""
    digmod.dig(str(repo), "m.alpha", cache=True)

    def fp():
        # cache/ and log/ are excluded: warming the pickle caches on read
        # is a SEPARATE pre-existing defect (fixed on feature/console via
        # query.read_only), and the usage log is personal-machine
        # telemetry. Everything this feature owns - evidence/ and meta/ -
        # is fingerprinted.
        c = repo / ".coord"
        # cache/ and log/: derived caches + personal telemetry (the
        # general read fence lives on feature/console via read_only).
        # docbindings.json: NOT a cache - it snapshots the hash a design
        # doc was written against, and drift is measured from it. A read
        # legitimately refreshes it; suppressing that made every binding
        # read permanently "fresh".
        skip = {"cache", "log"}
        return {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(c.rglob("*"))
                if p.is_file() and not (skip & set(p.parts))
                and p.name != "docbindings.json"}

    before = fp()
    getattr(query, fn)(str(repo), "m.alpha")
    assert fp() == before, f"{fn} wrote to .coord"


def test_read_surfaces_show_evidence_when_present(repo):
    digmod.dig(str(repo), "m.alpha", cache=True)
    for fn in ("before_edit", "show"):
        out = getattr(query, fn)(str(repo), "m.alpha")
        assert "evidence" in out, fn
        assert out["evidence"]["count"] > 0
        assert out["evidence"]["top"], "the section must name items"
        for item in out["evidence"]["top"]:
            assert "body" not in item, "bodies are fetched by ref, not dumped"


def test_no_evidence_section_when_nothing_cached(repo):
    out = query.before_edit(str(repo), "m.alpha")
    assert "evidence" not in out, "absent evidence must not fabricate a section"


# ------------------------------------------------------ verdict rendering

def _verdict_note(repo, ref_sha, text="this was a deliberate migration"):
    e = coord_of(repo)
    MetaStore(repo / ".coord").add(
        e.coord_id, "notes", f"VERDICT {ref_sha}: {text}",
        author="test", body_hash=e.body_hash)
    return e


def test_verdict_joins_its_evidence(repo):
    """The reader gets the author's own words AND the judgment, with the
    body stored once."""
    out = digmod.dig(str(repo), "m.alpha", cache=True)
    sha = out["candidates"][0]["short_sha"]
    subject = out["candidates"][0]["subject"]
    _verdict_note(repo, sha)
    b = query.before_edit(str(repo), "m.alpha")
    v = [k for k in b["knowledge"] if k.get("verdict")]
    assert len(v) == 1, "the note must be recognised as a verdict"
    verdict = v[0]["verdict"]
    assert verdict["judgment"] == "this was a deliberate migration"
    assert verdict["evidence"] is not None, "evidence is cached; join it"
    assert verdict["evidence"]["subject"] == subject
    assert verdict["evidence"]["ref"] == sha
    assert subject not in v[0]["text"], "the verdict must not RESTATE it"


def test_verdict_renders_degraded_when_evidence_cleared(repo):
    out = digmod.dig(str(repo), "m.alpha", cache=True)
    sha = out["candidates"][0]["short_sha"]
    _verdict_note(repo, sha)
    ev.clear(repo / ".coord")
    b = query.before_edit(str(repo), "m.alpha")
    v = [k for k in b["knowledge"] if k.get("verdict")]
    assert len(v) == 1, "the verdict SURVIVES - it is authored"
    assert v[0]["verdict"]["evidence"] is None
    assert v[0]["verdict"]["note"] == ev.UNCACHED_NOTE
    assert "re-dig" in v[0]["verdict"]["note"], "say how to restore it"
    assert v[0]["text"].startswith("VERDICT"), "text is untouched"


def test_re_digging_restores_the_join(repo):
    out = digmod.dig(str(repo), "m.alpha", cache=True)
    sha = out["candidates"][0]["short_sha"]
    _verdict_note(repo, sha)
    ev.clear(repo / ".coord")
    assert query.before_edit(str(repo), "m.alpha")["knowledge"][0]["verdict"][
        "evidence"] is None
    digmod.dig(str(repo), "m.alpha", cache=True)
    again = query.before_edit(str(repo), "m.alpha")
    v = [k for k in again["knowledge"] if k.get("verdict")][0]
    assert v["verdict"]["evidence"] is not None, "one re-dig restores it"


def test_verdict_against_a_pr_number_joins_too(repo, monkeypatch):
    monkeypatch.setattr(digmod, "_gh_ready", lambda r: ("o/r", None))
    monkeypatch.setattr(digmod, "_fetch_pr", lambda s, n, c: ("WHY: because.", None))
    digmod.dig(str(repo), "m.alpha", cache=True)
    _verdict_note(repo, "#11", "the PR explains the migration")
    b = query.before_edit(str(repo), "m.alpha")
    v = [k for k in b["knowledge"] if k.get("verdict")]
    assert v and v[0]["verdict"]["evidence"] is not None
    assert v[0]["verdict"]["evidence"]["source"] == "pr"


@pytest.mark.parametrize("text", [
    "a plain observation about this function",
    "VERDICT without a colon or ref",
    "the verdict is still out on this one",
    "VERDICTS: plural, not the form",
])
def test_free_text_notes_are_untouched(repo, text):
    """Some whys are born in a session, not a commit. That path must be
    byte-identical to its pre-feature behaviour."""
    e = coord_of(repo)
    MetaStore(repo / ".coord").add(e.coord_id, "notes", text,
                                   author="test", body_hash=e.body_hash)
    b = query.before_edit(str(repo), "m.alpha")
    k = [x for x in b["knowledge"] if x["text"] == text]
    assert len(k) == 1
    assert "verdict" not in k[0], f"{text!r} is a free-text note"
    assert k[0]["stale"] is False
    assert ev.parse_verdict(text) is None


# ------------------------------------------------- truncation ordering

def test_truncation_sacrifices_evidence_before_authored_knowledge(repo):
    """The ordering IS the contract: evidence is regenerable, authored
    knowledge is somebody's judgment and gone forever if dropped."""
    e = coord_of(repo)
    store = MetaStore(repo / ".coord")
    for i in range(6):
        store.add(e.coord_id, "notes", f"authored note {i} " + "x" * 400,
                  author="test", body_hash=e.body_hash)
    digmod.dig(str(repo), "m.alpha", cache=True)
    payload = query.before_edit(str(repo), "m.alpha")
    n_authored = len(payload["knowledge"])
    assert "evidence" in payload and n_authored >= 6

    capped = query.apply_read_cap(json.loads(json.dumps(payload)), cap=1500)

    assert "payload_capped" in capped
    assert "evidence" not in capped or capped["evidence"].get("truncated")
    trimmed = capped["payload_capped"]["trimmed"]
    assert any("evidence" in t for t in trimmed)
    if any("AUTHORED" in t for t in trimmed):
        assert trimmed.index(next(t for t in trimmed if "AUTHORED" in t)) > 0, \
            "evidence must be trimmed BEFORE authored knowledge"


def test_cap_leaves_a_generous_payload_alone(repo):
    digmod.dig(str(repo), "m.alpha", cache=True)
    payload = query.before_edit(str(repo), "m.alpha")
    out = query.apply_read_cap(json.loads(json.dumps(payload)), cap=500_000)
    assert "payload_capped" not in out
    assert "evidence" in out


# ------------------------------------------------------------- surfaces

def test_cli_dig_cache_and_evidence_roundtrip(repo):
    r = cli("dig", repo, "m.alpha", "--cache")
    assert r.returncode == 0, r.stderr[-300:]
    assert "cached" in r.stdout
    r2 = cli("evidence", repo, "m.alpha")
    assert r2.returncode == 0, r2.stderr[-300:]
    assert "records, current through" in r2.stdout
    r3 = cli("evidence", repo, "--clear")
    assert r3.returncode == 0
    assert "authored knowledge in .coord/meta is untouched" in r3.stdout


def test_mcp_dig_exposes_cache_and_defaults_off(repo):
    from memway.mcp import TOOLS
    t = next(t for t in TOOLS if t["name"] == "memway_dig")
    assert "cache" in t["inputSchema"]["properties"]
    assert t["inputSchema"]["required"] == ["ref"], "cache must be optional"
    t["fn"](str(repo), {"ref": "m.alpha"})
    assert not ev.evidence_root(repo / ".coord").exists(), \
        "MCP dig without cache:true must not write"
    t["fn"](str(repo), {"ref": "m.alpha", "cache": True})
    assert ev.evidence_root(repo / ".coord").exists()
