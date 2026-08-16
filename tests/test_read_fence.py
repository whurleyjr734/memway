"""The read fence, across every read surface at once.

Three features each discovered a piece of this separately - viz found the
coordinates cache, then the edges cache; the console found docbindings
being rewritten on every briefing. Each fix was verified only on its own
branch, and the union turned out to be 2/7.

So the fence lives here, in one place, measured the same way for every
read: fingerprint .coord, run the read, fingerprint again. A read that
changes a byte is not a read.

`log/` is excluded - the flight recorder is personal-machine telemetry
and is gitignored as such.

WHY THE WARM-UP CALL: `docbindings.json` is a snapshot BASELINE, not a
cache (see harvest.harvest_docs). Establishing it on a map that has never
had one is a legitimate first write. What must never happen is a read
mutating an already-established map - that is the steady state every
briefing after the first one lives in, and what this asserts.
"""

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import query
from memway.dig import dig
from memway.viz import viz


SRC = '''"""Module m."""


def alpha(x):
    """Alpha."""
    return x + 1


class Thing:
    """A thing."""

    def run(self, x):
        return alpha(x)
'''


@pytest.fixture(scope="module")
def mapped(tmp_path_factory):
    R = tmp_path_factory.mktemp("fence") / "proj"
    R.mkdir()
    subprocess.run(["git", "-C", str(R), "init", "-q", "-b", "main"], check=True)
    (R / "m.py").write_text(SRC)
    subprocess.run(["git", "-C", str(R), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t",
                    "-c", "user.name=T", "commit", "-qm", "seed",
                    "--no-gpg-sign"], check=True)
    r = subprocess.run([sys.executable, "-m", "memway.cli", "init", str(R)],
                       capture_output=True, text=True, cwd=str(HERE))
    assert r.returncode == 0, r.stderr[-400:]
    return R


def fingerprint(repo: Path) -> dict:
    return {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((repo / ".coord").rglob("*"))
            if p.is_file() and "log" not in p.parts}


READS = {
    "before_edit": lambda r: query.before_edit(str(r), "m.alpha"),
    "show":        lambda r: query.show(str(r), "m.alpha"),
    "summary":     lambda r: query.summary(str(r)),
    "at":          lambda r: query.at(str(r), "m.py:4"),
    "lineage":     lambda r: query.lineage(str(r), "m.alpha"),
    "viz":         lambda r: viz(str(r), str(r / "map.html")),
    "dig":         lambda r: dig(str(r), "m.alpha"),
    # Enrolled in 0.54.1. verify_change was a documented exception - it
    # re-indexed and saved, and tests/test_verify_query recorded that as
    # deliberate. It is a pure read now, and this is where the guarantee
    # lives. attention was never enrolled because it was never a query.
    "verify_change": lambda r: query.verify_change(str(r)),
    "attention":     lambda r: query.attention(str(r)),
}


@pytest.mark.parametrize("name", sorted(READS))
def test_read_leaves_coord_byte_identical(mapped, tmp_path, name):
    """Every read surface, same measurement. Do not exempt one."""
    work = tmp_path / "repo"
    shutil.copytree(mapped, work)
    fn = READS[name]
    fn(work)                       # establish any first-call baseline
    before = fingerprint(work)
    assert before, "the fixture must have a map"
    fn(work)                       # the steady-state read under test
    after = fingerprint(work)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert not changed, f"{name} wrote {sorted(changed)}"


def test_reads_do_not_accumulate_writes(mapped, tmp_path):
    """Ten reads of every kind must be as inert as one."""
    work = tmp_path / "repo"
    shutil.copytree(mapped, work)
    for fn in READS.values():
        fn(work)
    before = fingerprint(work)
    for _ in range(10):
        for fn in READS.values():
            fn(work)
    assert fingerprint(work) == before


def test_query_ctx_never_warms_a_pickle_cache():
    """The specific regression: _ctx used write_cache=not _READ_ONLY, so
    every CLI and MCP read warmed both caches - only the console's HTTP
    handlers were inside read_only(). The fence was 2/7 and looked fixed."""
    src = (HERE / "memway" / "query.py").read_text()
    assert "ix.load_existing(write_cache=False)" in src
    assert "EdgeBuilder.load(coord, write_cache=False)" in src
    assert "write_cache=not _READ_ONLY" not in src, \
        "a read must not depend on the caller remembering to be read-only"


def test_docbindings_is_written_only_when_it_changes():
    """It is a snapshot baseline, so it must still be WRITABLE - killing
    the write entirely made every design-doc binding read permanently
    fresh. Write-if-changed keeps drift detection and the fence both."""
    src = (HERE / "memway" / "harvest.py").read_text()
    assert "if not path.exists() or path.read_text() != new:" in src
    assert "not a cache. It snapshots the entity" in src, \
        "the WHY must stay recorded"


# ------------------------------------- the single core, extended to WRITES

def test_every_write_path_stamps_identically(mapped, tmp_path):
    """THE DRIFT THIS PREVENTS: `memway meta` stamped body_hash while the
    MCP agent_meta stamped logic_hash, so the same note written from two
    surfaces decayed at different rates - a docstring edit staled the
    CLI's copy and left the agent's fresh. Found by a smoke test on the
    shipped wheel, not by the suite."""
    from memway.metadata import MetaStore, stamp_for, accepted_for
    from memway.indexer import Indexer
    work = tmp_path / "repo"
    shutil.copytree(mapped, work)

    r = subprocess.run([sys.executable, "-m", "memway.cli", "meta", str(work),
                        "m.alpha", "notes", "via cli", "--author", "cli"],
                       capture_output=True, text=True, cwd=str(HERE))
    assert r.returncode == 0, r.stderr[-300:]
    query.agent_meta(str(work), "m.alpha", "notes", "via mcp")

    ix = Indexer(work, work / ".coord"); ix.load_existing()
    e = ix.resolve("m.alpha")
    entries = MetaStore(work / ".coord").read(e.coord_id, "notes")
    by = {x["text"]: x["body_hash"] for x in entries}
    assert "via cli" in by and "via mcp" in by
    assert by["via cli"] == by["via mcp"], \
        "two surfaces, two stamps - the write side has drifted again"
    assert by["via cli"] == stamp_for(e)
    assert stamp_for(e) == (e.logic_hash or e.body_hash), "logic hash FIRST"


def test_docstring_edit_leaves_both_writers_fresh(mapped, tmp_path):
    from memway.metadata import MetaStore, accepted_for
    from memway.indexer import Indexer
    work = tmp_path / "repo"
    shutil.copytree(mapped, work)
    subprocess.run([sys.executable, "-m", "memway.cli", "meta", str(work),
                    "m.alpha", "notes", "via cli", "--author", "cli"],
                   capture_output=True, text=True, cwd=str(HERE), check=True)
    query.agent_meta(str(work), "m.alpha", "notes", "via mcp")

    src = (work / "m.py").read_text()
    (work / "m.py").write_text(src.replace('"""Alpha."""', '"""Alpha, revised."""'))
    subprocess.run([sys.executable, "-m", "memway.cli", "index", str(work)],
                   capture_output=True, text=True, cwd=str(HERE), check=True)

    ix = Indexer(work, work / ".coord"); ix.load_existing()
    e = ix.resolve("m.alpha")
    got = MetaStore(work / ".coord").read(e.coord_id, "notes",
                                          current_hash=accepted_for(e))
    stale = {x["text"]: bool(x.get("stale")) for x in got}
    assert stale == {"via cli": False, "via mcp": False}, \
        f"a docstring edit must not stale either writer: {stale}"


def test_logic_edit_stales_both_writers(mapped, tmp_path):
    """The control: without it, 'nothing ever goes stale' would pass."""
    from memway.metadata import MetaStore, accepted_for
    from memway.indexer import Indexer
    work = tmp_path / "repo"
    shutil.copytree(mapped, work)
    subprocess.run([sys.executable, "-m", "memway.cli", "meta", str(work),
                    "m.alpha", "notes", "via cli", "--author", "cli"],
                   capture_output=True, text=True, cwd=str(HERE), check=True)
    query.agent_meta(str(work), "m.alpha", "notes", "via mcp")

    src = (work / "m.py").read_text()
    (work / "m.py").write_text(src.replace("return x + 1", "return x + 99"))
    subprocess.run([sys.executable, "-m", "memway.cli", "index", str(work)],
                   capture_output=True, text=True, cwd=str(HERE), check=True)

    ix = Indexer(work, work / ".coord"); ix.load_existing()
    e = ix.resolve("m.alpha")
    got = MetaStore(work / ".coord").read(e.coord_id, "notes",
                                          current_hash=accepted_for(e))
    stale = {x["text"]: bool(x.get("stale")) for x in got}
    assert stale == {"via cli": True, "via mcp": True}, \
        f"a behaviour change must stale both: {stale}"


def test_no_module_reimplements_the_hash_rule():
    """Structural: the rule lives in metadata and nowhere else."""
    import re
    for f in (HERE / "memway").glob("*.py"):
        if f.name == "metadata.py":
            continue
        src = f.read_text()
        assert "body_hash=e.body_hash" not in src, f"{f.name} inlines a stamp"
        assert not re.search(r'current_hash=\{getattr\(', src), \
            f"{f.name} inlines the accepted-hash set"


# ---------------------------------------------------------------- arity

@pytest.mark.parametrize("cmd", ["lineage", "dig", "viz", "meta", "show",
                                 "evidence", "at", "index", "pull"])
def test_bare_command_exits_2_with_usage_not_a_traceback(cmd):
    r = subprocess.run([sys.executable, "-m", "memway.cli", cmd],
                       capture_output=True, text=True, cwd=str(HERE))
    out = r.stdout + r.stderr
    assert "Traceback" not in out, f"{cmd} raised: {out[-200:]}"
    assert r.returncode == 2, f"{cmd} exited {r.returncode}, expected 2"
    assert "usage:" in out, f"{cmd} printed no usage line"
    assert cmd in out


def test_a_real_error_inside_a_command_is_not_disguised_as_usage(tmp_path):
    """bind() checks arity BEFORE the call, so a TypeError from inside a
    working command still surfaces as itself."""
    r = subprocess.run([sys.executable, "-m", "memway.cli", "show",
                        str(tmp_path / "nonexistent"), "x"],
                       capture_output=True, text=True, cwd=str(HERE))
    assert r.returncode != 2 or "usage:" not in (r.stdout + r.stderr), \
        "a missing repo must not be reported as a usage error"


def test_parser_hint_is_shell_safe():
    src = (HERE / "memway" / "cli.py").read_text()
    assert "pip install 'memway[languages]'" in src, "must be QUOTED for zsh"
    assert "pip install -e ." not in src, "dev advice must not ship to users"


# ---------------------------------------- no query may skip the fence

def test_every_json_query_is_enrolled_in_this_fence():
    """The structural half. verify_change spent two releases writing five
    files because nothing forced a new query into READS - it was added to
    QUERIES, documented as an exception, and that was the end of it.

    A count would not have caught it either: the table had the right
    number of entries, just not the right names. So this compares the two
    sets and names what is missing.
    """
    queries = {q.replace("-", "_") for q in query.QUERIES}
    enrolled = set(READS)
    missing = sorted(queries - enrolled)
    assert not missing, (
        f"these --json queries are not in READS and so are never checked "
        f"for writes: {missing}\n"
        f"queries: {sorted(queries)}\nenrolled: {sorted(enrolled)}")


def test_the_fence_covers_more_than_the_queries():
    """Guard the guard: READS must not shrink to exactly the query list.
    viz, dig and console are read surfaces that are not --json queries,
    and dropping them would leave the test above passing."""
    assert len(READS) > len(query.QUERIES), sorted(READS)
    assert {"viz", "dig"} <= set(READS)
