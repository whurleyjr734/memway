"""The map must be a pure function of the tree.

Everything the collaboration story needs rests on this. If two people
indexing the same commit get different bytes, then `git diff .coord` can
never mean "the map is stale", every parallel index conflicts, and the
map cannot be said to follow git history at all.

It did not hold until 0.54.0. `_sketch` hashed token shingles with
builtin hash(), which Python randomizes per process, and sketches are
PERSISTED - so two fresh clones of one sha produced byte-identical
output except `sketch`, which differed on 888/888 entities.

THESE TESTS MUST NOT PIN PYTHONHASHSEED. Pinning it is what made the bug
invisible; a test that pins it proves only that the machine can be made
to agree with itself. Each one instead checks that the child processes
really did get DIFFERENT randomization, so a green result means the
property held in the wild rather than that the wild was switched off.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def _env():
    """A child environment with randomization LEFT ON."""
    e = dict(os.environ)
    e.pop("PYTHONHASHSEED", None)
    return e


def _run(*args, cwd=None):
    return subprocess.run([sys.executable, "-m", "memway.cli", *args],
                          capture_output=True, text=True, env=_env(),
                          cwd=cwd or str(HERE))


def _seed_of(cwd):
    """What this child's string hashing actually does, as a witness."""
    r = subprocess.run([sys.executable, "-c",
                        "print(hash('memway-probe'))"],
                       capture_output=True, text=True, env=_env(), cwd=cwd)
    return r.stdout.strip()


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def _commit(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", msg, "--no-gpg-sign")


SRC = {
    "alpha.py": '''
def collect_rows(source, limit=100):
    """Read rows from source."""
    out = []
    for i, row in enumerate(source):
        if i >= limit:
            break
        if row.get("ok"):
            out.append(row)
    return out


class Ledger:
    """A ledger."""

    def __init__(self, rows):
        self.rows = rows

    def total(self, rate):
        t = 0
        for r in self.rows:
            t += r["amount"] * rate
        return round(t, 2)
''',
    "beta.py": '''
from alpha import collect_rows


def summarize(source):
    """Summarize a source."""
    rows = collect_rows(source)
    return {"n": len(rows), "sum": sum(r["amount"] for r in rows)}


def normalize(value, lo=0.0, hi=1.0):
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value
''',
}

DERIVED = ["index/coordinates.json", "index/edges.json",
           "index/raw_edges.json", "index/parse_cache.json",
           "metrics/metrics.json", "manifest.json"]


@pytest.fixture
def twin(tmp_path):
    """Two independent checkouts of one identical tree."""
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        for name, body in SRC.items():
            (d / name).write_text(body)
        _git(d, "init", "-q", "-b", "main")
        _commit(d, "one")
    return a, b


def test_two_clones_of_one_tree_produce_identical_maps(twin):
    """THE determinism pin. Byte-for-byte, in the wild."""
    a, b = twin
    ra, rb = _run("init", str(a)), _run("init", str(b))
    assert ra.returncode == 0, ra.stderr[-400:]
    assert rb.returncode == 0, rb.stderr[-400:]

    # Guard the guard: if both children happened to hash identically,
    # this test could pass without the property holding.
    sa, sb = _seed_of(str(a)), _seed_of(str(b))
    assert sa and sb, (sa, sb)
    assert sa != sb, (
        "both children hashed identically, so randomization was NOT live "
        "and this test proved nothing - is PYTHONHASHSEED set?")

    differing = [f for f in DERIVED
                 if (a / ".coord" / f).read_bytes()
                 != (b / ".coord" / f).read_bytes()]
    assert not differing, (
        f"indexing is not deterministic; these differ between two clones "
        f"of one tree: {differing}")


def test_the_sketch_field_specifically_is_stable(twin):
    """Named separately because `sketch` is where it broke, and a whole-file
    comparison would not say which field moved if it regresses."""
    a, b = twin
    _run("init", str(a))
    _run("init", str(b))
    da = json.loads((a / ".coord/index/coordinates.json").read_text())
    db = json.loads((b / ".coord/index/coordinates.json").read_text())
    assert set(da) == set(db)
    bad = [da[k]["qualname"] for k in da if da[k]["sketch"] != db[k]["sketch"]]
    assert not bad, f"{len(bad)}/{len(da)} sketches differ across processes"
    assert any(da[k]["sketch"] and any(da[k]["sketch"]) for k in da), \
        "every sketch is empty - this test would pass on a broken _sketch"


def test_the_map_records_which_shingle_hash_built_it(twin):
    from memway.indexer import SKETCH_VERSION, stored_sketch_version
    a, _ = twin
    _run("init", str(a))
    assert stored_sketch_version(a / ".coord") == SKETCH_VERSION
    man = json.loads((a / ".coord/manifest.json").read_text())
    assert man["sketch_version"] == SKETCH_VERSION
    assert man.get("format"), "the stamp must be additive, not a rewrite"


def test_an_unstamped_map_is_read_as_generation_one(tmp_path):
    """Absent means 1, never 'current'. A pre-0.54 map that claimed
    comparability is the one lie this mechanism exists to prevent."""
    from memway.indexer import stored_sketch_version
    c = tmp_path / ".coord"
    c.mkdir()
    (c / "manifest.json").write_text('{"format": "memway/0.1"}')
    assert stored_sketch_version(c) == 1
    (c / "manifest.json").write_text("not json at all")
    assert stored_sketch_version(c) == 1


# --------------------------------------------------- the lineage regression

RENAME_BEFORE = '''
def compute_totals(rows, rate):
    """Sum the rows and apply the rate."""
    total = 0
    for r in rows:
        if r.get("active"):
            total += r["amount"] * rate
        else:
            total += r["amount"]
    return round(total, 2)
'''

RENAME_AFTER = '''
def ledger_rollup(rows, rate, cutoff=None):
    """Sum the rows and apply the rate."""
    total = 0
    seen = []
    for r in rows:
        if cutoff and r["amount"] > cutoff:
            continue
        seen.append(r)
        if r.get("active"):
            total += r["amount"] * rate
        else:
            total += r["amount"]
    if not seen:
        return 0.0
    return round(total, 2)
'''


@pytest.mark.parametrize("run", range(5))
def test_a_heavily_edited_rename_is_never_silently_deleted(tmp_path, run):
    """The regression, repeated 5x to catch seed-dependence structurally.

    This is the case that ONLY the minhash can see: the name barely
    resembles itself, the shape changed, the signature changed. Under
    randomized hashing it scored ~0 on the one signal that could have
    matched it and was recorded as `deleted` with author="auto" - a
    confident verdict built on a measurement that never happened, with
    the entity's knowledge orphaned behind it.

    Parametrized rather than looped so a failure names WHICH run failed;
    a loop would hide whether it broke once or every time.
    """
    repo = tmp_path / f"r{run}"
    repo.mkdir()
    (repo / "m.py").write_text(RENAME_BEFORE)
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "one")
    assert _run("init", str(repo)).returncode == 0

    (repo / "m.py").write_text(RENAME_AFTER)
    _commit(repo, "rename with heavy edits")
    r = _run("index", str(repo))
    assert r.returncode == 0, r.stderr[-400:]

    entries = [json.loads(l) for l in
               (repo / ".coord/lineage/lineage.jsonl").read_text().splitlines()
               if l.strip()]
    assert entries, "no lineage recorded at all"
    kinds = {e["kind"] for e in entries}
    assert "deleted" not in kinds, (
        f"the rename was recorded as a deletion: {entries}")

    linked = [e for e in entries if e["old"] and e["new"]]
    assert linked, f"no entry links the old coordinate to the new one: {entries}"
    assert all(e["author"] == "pending-review" for e in linked), (
        f"a heavily-edited rename must ask for confirmation, not assert: "
        f"{linked}")
    assert any("ledger_rollup" in (e.get("note") or "") for e in linked), linked


# ------------------------------------------------------ the migration guard

def test_a_pre_054_map_does_not_get_confident_deletions(tmp_path):
    """Half-migrated is the dangerous state: old sketches on disk, new hash
    in the code. Scoring an incomparable signal as 0.0 reproduces exactly
    the bug this release fixes, so the guard must catch it."""
    repo = tmp_path / "old"
    repo.mkdir()
    (repo / "m.py").write_text(RENAME_BEFORE)
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "one")
    assert _run("init", str(repo)).returncode == 0

    # forge a pre-0.54 map: the stamp is what tells them apart
    man_p = repo / ".coord/manifest.json"
    man = json.loads(man_p.read_text())
    del man["sketch_version"]
    man_p.write_text(json.dumps(man))

    (repo / "m.py").write_text(RENAME_AFTER)
    _commit(repo, "rename with heavy edits")
    r = _run("index", str(repo))
    assert r.returncode == 0, r.stderr[-400:]
    assert "sketch generation changed" in r.stdout, r.stdout
    assert "not comparable" in r.stdout, r.stdout

    entries = [json.loads(l) for l in
               (repo / ".coord/lineage/lineage.jsonl").read_text().splitlines()
               if l.strip()]
    auto_deletes = [e for e in entries
                    if e["kind"] == "deleted" and e["author"] == "auto"]
    assert not auto_deletes, (
        f"a migrating index asserted a deletion it could not know: "
        f"{auto_deletes}")
    assert json.loads(man_p.read_text())["sketch_version"] == 2, \
        "the migrating index must stamp the map so the NEXT one is normal"


def test_the_migration_guard_does_not_fire_on_a_current_map(tmp_path):
    """The other half: the guard must be quiet in the normal case, or it
    is just a permanent warning nobody reads."""
    repo = tmp_path / "cur"
    repo.mkdir()
    (repo / "m.py").write_text(RENAME_BEFORE)
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "one")
    _run("init", str(repo))
    (repo / "m.py").write_text(RENAME_AFTER)
    _commit(repo, "two")
    r = _run("index", str(repo))
    assert "sketch generation changed" not in r.stdout, r.stdout


# ------------------------------- excluded is not the same as scored zero

class _Ent:
    """Minimal stand-in: score_pair reads exactly these five fields."""

    def __init__(self, qualname, sketch, shape_hash, signature, loc):
        self.qualname = qualname
        self.sketch = sketch
        self.shape_hash = shape_hash
        self.signature = signature
        self.loc = loc


def test_an_unmeasured_signal_is_excluded_not_scored_zero():
    """The renormalization, isolated.

    This exists because the first falsification of it FAILED to bite:
    replacing `exclude jac` with `score jac as 0.0` left every
    end-to-end test green, since the guard's three other effects carried
    them. An untested branch that looks tested is worse than an absent
    one, so the arithmetic gets its own witness.

    Zeroing multiplies every score by 0.70 (the surviving weight mass),
    which silently moves the 0.33 and 0.55 thresholds for every pair.
    """
    from memway.lineage import score_pair, _SIGNAL_WEIGHTS

    # Chosen to sit clear of the 0.33 floor on BOTH sides (0.418 vs 0.293),
    # not merely on one: a fixture at 0.334 straddles too, and would flip on
    # any weight tweak while still looking like it tested something.
    o = _Ent("m.compute_totals", [1] * 48, "S1", "(rows, rate)", 12)
    n = _Ent("m.ledger_rollup", [2] * 48, "S2", "(rows, rate)", 12)

    excluded, _, jac, _ = score_pair(o, n, use_sketch=False)
    assert jac == 0.0, "jac must not be computed when it cannot be compared"

    surviving = 1.0 - _SIGNAL_WEIGHTS["jac"]
    zeroed = excluded * surviving          # what the buggy form would yield
    assert abs(surviving - 0.70) < 1e-9, _SIGNAL_WEIGHTS
    assert excluded > zeroed, (excluded, zeroed)
    # and it is not a rounding-level difference: a third of the score
    assert excluded - zeroed > 0.09, (excluded, zeroed)

    # the pair straddles the match floor: kept by excluding, lost by zeroing
    assert zeroed < 0.33 <= excluded, (
        f"fixture no longer straddles the 0.33 floor "
        f"(excluded={excluded:.3f}, zeroed={zeroed:.3f}) - it must, or this "
        f"test stops discriminating the two forms")


def test_scoring_with_the_sketch_available_is_unchanged():
    """The renormalization must be a no-op in the normal case - all five
    weights present sum to 1.0, so nothing moves."""
    from memway.lineage import score_pair
    o = _Ent("m.alpha", [7] * 48, "S1", "(a, b)", 10)
    n = _Ent("m.alpha", [7] * 48, "S1", "(a, b)", 10)
    sc, name_s, jac, shape = score_pair(o, n, use_sketch=True)
    assert name_s == 1.0 and jac == 1.0 and shape == 1.0
    assert abs(sc - 1.0) < 1e-9, sc
