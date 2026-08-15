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
