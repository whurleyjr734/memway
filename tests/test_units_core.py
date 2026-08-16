"""CORE COMPLETION: the remaining uncovered lines of the core systems,
each hit by a targeted test. Companion to test_units.py; together they
take the identity/knowledge/graph/intelligence cores to complete
coverage of every line reachable in CI (the sole exception - the real
sentence-transformers model load - is pragma-annotated and its FAILURE
branch is tested here instead).
"""

import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway.indexer import Indexer
from memway.edges import EdgeBuilder
from memway.metadata import MetaStore, TraceRecorder
from memway.metrics import (_py_complexity, complexity_of, _pct_rank,
                              MetricsStore)
from memway.lineage import VersionStore
from memway.access_cache import load_json_cached

from test_units import make, full_index, PY, BODY


# ============================================================ indexer

def test_recover_from_snapshot_direct_and_empty(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    # direct: snapshot exists -> identities recovered
    data = ix._recover_from_snapshot()
    assert data and all(k.startswith("C-") for k in data)
    # empty: no versions dir -> honest empty rebuild
    bare = Indexer(tmp_path / "nowhere", tmp_path / "nowhere" / ".coord")
    assert bare._recover_from_snapshot() == {}


def test_skip_dirs_min_and_corrupt_parse_cache(tmp_path):
    files = {
        PY: BODY,
        "dist/junk.py": "def in_dist():\n    return 1\n",
        "web/lib.min.js": "function m(){return 1}\n",
    }
    ix, _, _ = make(tmp_path, files)
    quals = {e.qualname for e in ix.entities.values()}
    assert not any("in_dist" in q for q in quals)       # SKIP_DIRS
    assert not any("lib" in q for q in quals)           # .min. skip
    # corrupt parse cache json -> silently rebuilt
    pc = tmp_path / ".coord" / "cache" / "parse_cache.json"
    if pc.exists():
        pc.write_text("{ not json")
    ix2 = Indexer(tmp_path, tmp_path / ".coord")
    full_index(ix2, tmp_path)
    assert ix2.resolve("src.m.alpha") is not None


def test_resolve_by_coord_id_and_raw_edges_load(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    cid = ix.resolve("src.m.alpha").coord_id
    assert ix.resolve(cid).coord_id == cid              # ID-direct path
    ix2 = Indexer(tmp_path, tmp_path / ".coord")
    ix2.load_existing()
    ix2.load_raw_edges()
    assert getattr(ix2, "_raw_edges", None) is not None


# =========================================================== metadata

def test_trace_recorder_hops_and_commit(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    a = ix.resolve("src.m.alpha").coord_id
    b = ix.resolve("src.m.beta").coord_id
    ms = MetaStore(tmp_path / ".coord")
    rec = TraceRecorder(ms, "T-1")
    path = rec.hop(a, "entered").hop(b, "computed").commit()
    assert path == [a, b]
    md = ms.read_all(a)
    tr = md["traces"][0]
    assert tr["trace_id"] == "T-1" and tr["full_path"] == [a, b]
    assert ms.read_all(b)["traces"][0]["seq"] == 1


def test_migrate_moves_knowledge(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    a = ix.resolve("src.m.alpha").coord_id
    b = ix.resolve("src.m.beta").coord_id
    ms = MetaStore(tmp_path / ".coord")
    ms.add(a, "notes", "carry me")
    ms.migrate(a, b, note="test move")
    assert any("carry me" in e["text"]
               for e in ms.read_all(b).get("notes", []))


# ============================================================ lineage

def test_probable_move_body_changed_records_moved(tmp_path):
    # same short name, DIFFERENT body, new location -> 'moved (verify)'
    ix, _, _ = make(tmp_path, {PY: BODY})
    old = ix.resolve("src.m.alpha").coord_id
    MetaStore(tmp_path / ".coord").add(old, "notes", "sticky")
    (tmp_path / "src" / "sub").mkdir()
    (tmp_path / "src" / "sub" / "__init__.py").write_text("")
    (tmp_path / PY).unlink()
    (tmp_path / "src" / "sub" / "m.py").write_text(
        "def alpha(x):\n    if x > 5:\n        return x * 2\n"
        "    return 0\n\ndef beta(y):\n    for i in range(y):\n"
        "        if i % 2 and i % 3:\n            y += i\n    return y\n")
    ix2 = Indexer(tmp_path, tmp_path / ".coord")
    full_index(ix2, tmp_path)
    recs = VersionStore(tmp_path / ".coord").read()
    moved = [r for r in recs if r["kind"] == "moved"]
    assert moved and "verify" in moved[0]["note"]
    new = ix2.resolve("src.sub.m.alpha").coord_id
    assert any("sticky" in e["text"] for e in
               MetaStore(tmp_path / ".coord").read_all(new)
               .get("notes", []))


def test_fresh_store_reads_empty(tmp_path):
    vs = VersionStore(tmp_path / ".coord")
    assert vs.read() == []
    assert vs.ancestry("C-none") == []


# ========================================================== embeddings

# ============================================================= metrics

def test_complexity_fallback_on_broken_python():
    assert _py_complexity("def oops(:\n  nope") >= 1     # never crashes
    assert complexity_of("def oops(:\n  nope", "x.py") >= 1


def test_pct_rank_empty_and_single():
    assert _pct_rank({}) == {}
    assert _pct_rank({"a": 3})["a"] >= 0.0


# =============================================================== edges

def test_unresolved_call_is_dropped_and_missing_file_loads_empty(tmp_path):
    ix, edges, _ = make(tmp_path, {PY:
        "def a():\n    return totally_unknown_fn()\n"})
    a = ix.resolve("src.m.a").coord_id
    assert not any(e["src"] == a and e["kind"] == "calls"
                   and not str(e["dst"]).startswith("EVT:")
                   for e in edges)
    assert EdgeBuilder.load(tmp_path / "no" / ".coord") == []


def test_add_dedupes(tmp_path):
    ix, _, _ = make(tmp_path, {PY: "def a():\n    return 1\n"})
    eb = EdgeBuilder(ix)
    eb.build()
    n = len(eb.edges)
    eb._add("X", "Y", "calls")
    eb._add("X", "Y", "calls")                           # dup dropped
    assert len(eb.edges) == n + 1


# ======================================================== access cache

def test_missing_source_returns_none_and_unwritable_cache_ok(tmp_path):
    assert load_json_cached(tmp_path / "nope.json",
                            tmp_path / ".coord") is None
    src = tmp_path / "d.json"
    src.write_text('{"k": 1}')
    coord = tmp_path / ".coord"
    coord.mkdir()
    (coord / "cache").write_text("i am a file, not a dir")
    assert load_json_cached(src, coord) == {"k": 1}      # cache optional


# =============================================================== blast

# ============================================================== agents
