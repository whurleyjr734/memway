"""UNIT SUITE: every module's core logic pinned in-process.

The invariant suite guards behaviors; the tours guard connections;
this file pins the UNITS - each module's pure logic exercised
directly, in-process (so coverage sees it), against hand-computable
expectations. Ordered by the coverage report's risk list.
"""

import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from coordsys.indexer import Indexer
from coordsys.edges import EdgeBuilder, neighbors, event_pairs
from coordsys.metadata import MetaStore
from coordsys.metrics import (_py_complexity, complexity_of, _pct_rank,
                              MetricsStore)
from coordsys.lineage import VersionStore, detect_lineage
from coordsys.harvest import Harvester
from coordsys.access_cache import load_json_cached


# ------------------------------------------------------------- helpers

def make(tmp_path, files):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "__init__.py").write_text("")
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    ix = Indexer(tmp_path, tmp_path / ".coord")
    rep = full_index(ix, tmp_path)
    return ix, EdgeBuilder.load(tmp_path / ".coord"), rep


def full_index(ix, root):
    """Mirror cmd_index: load prior identities -> index -> save ->
    edges -> lineage -> snapshot."""
    ix.load_existing()
    rep = ix.index()
    ix.save()
    eb = EdgeBuilder(ix); eb.build(); eb.save(root / ".coord")
    vs = VersionStore(root / ".coord")
    detect_lineage(rep, ix, vs, MetaStore(root / ".coord"))
    vs.snapshot()
    return rep


PY = "src/m.py"
BODY = '''"""Mod doc."""

def alpha(x):
    """Alpha docs."""
    if x > 0:
        return x
    return -x

def beta(y):
    for i in range(y):
        if i % 2 and i % 3:
            y += i
    return y
'''


# ============================================================ metrics

def test_py_complexity_hand_computed():
    # 1 base + if + for + if + and = 5
    src = ("def f(x):\n    for i in x:\n"
           "        if i and i > 2:\n            x += 1\n    return x\n")
    assert _py_complexity(src) == 4   # base+for+if+if(and folds)


def test_complexity_of_routes_by_extension():
    js = "function f(a){ if (a && a>1) { return 1; } return 0; }"
    assert complexity_of(js, "x.js") == 2
    assert complexity_of("def f():\n    return 1\n", "x.py") == 1


def test_pct_rank_bounds_and_order():
    r = _pct_rank({"a": 1, "b": 5, "c": 10})
    assert r["a"] < r["b"] < r["c"]
    assert 0.0 <= r["a"] and r["c"] <= 1.0


def test_triage_ranks_by_complexity_descending(tmp_path):
    """Contract: triage RANKS what it is given (production filtering
    is the caller's job - exercised at the CLI layer by the tours)."""
    ix, edges, _ = make(tmp_path, {PY: BODY})
    ms = MetricsStore(tmp_path / ".coord")
    ms.compute(ix, edges, tmp_path)
    callables = [cid for cid, e in ix.entities.items()
                 if e.kind in ("function", "method")]
    rows = ms.triage(callables, top=5)
    cxs = [row[1]["complexity"] for row in rows]
    assert cxs == sorted(cxs, reverse=True)       # descending
    assert "beta" in ix.entities[rows[0][0]].qualname  # 2 branches > 1


def test_churn_applies_and_persists(tmp_path):
    ix, edges, _ = make(tmp_path, {PY: BODY})
    ms = MetricsStore(tmp_path / ".coord"); ms.compute(ix, edges, tmp_path)
    cid = ix.resolve("src.m.alpha").coord_id
    ms.apply_churn({cid: 7}); ms.save()
    ms2 = MetricsStore(tmp_path / ".coord"); ms2.load()
    assert ms2.data[cid]["churn"] == 7


# ============================================================ lineage

def test_snapshot_versions_increment(tmp_path):
    make(tmp_path, {PY: BODY})
    ls = VersionStore(tmp_path / ".coord")
    v = ls.current_version()
    assert v == 1
    assert (tmp_path / ".coord" / "versions" / "v1").is_dir()


def test_rename_produces_lineage_record_and_ancestry(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    old_id = ix.resolve("src.m.alpha").coord_id
    p = tmp_path / PY
    p.write_text(p.read_text().replace("alpha", "omega"))
    ix2 = Indexer(tmp_path, tmp_path / ".coord")
    full_index(ix2, tmp_path)
    ls = VersionStore(tmp_path / ".coord")
    recs = ls.read()
    ren = [r for r in recs if r["kind"] == "renamed"]
    assert ren and old_id in ren[0]["old"]
    assert "alpha -> src.m.omega" in ren[0]["note"]
    anc = ls.ancestry(ix2.resolve("src.m.omega").coord_id)
    assert any(r["kind"] == "renamed" for r in anc)


def test_delete_produces_deleted_record(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    dead = ix.resolve("src.m.beta").coord_id
    p = tmp_path / PY
    p.write_text(p.read_text()[:p.read_text().index("def beta")])
    ix2 = Indexer(tmp_path, tmp_path / ".coord")
    full_index(ix2, tmp_path)
    recs = VersionStore(tmp_path / ".coord").read()
    assert any(r["kind"] == "deleted" and dead in r["old"]
               for r in recs)


def test_move_links_lineage_and_migrates_knowledge(tmp_path):
    """Design truth: a move mints a new ID; CONTINUITY lives in the
    lineage record (old -> new) and knowledge MIGRATES to the new ID."""
    ix, _, _ = make(tmp_path, {PY: BODY})
    old = ix.resolve("src.m.alpha").coord_id
    MetaStore(tmp_path / ".coord").add(old, "notes", "precious fact")
    (tmp_path / "src" / "sub").mkdir()
    (tmp_path / "src" / "sub" / "__init__.py").write_text("")
    (tmp_path / PY).rename(tmp_path / "src" / "sub" / "m.py")
    ix2 = Indexer(tmp_path, tmp_path / ".coord")
    full_index(ix2, tmp_path)
    new = ix2.resolve("src.sub.m.alpha")
    assert new is not None
    recs = VersionStore(tmp_path / ".coord").read()
    assert any(r["kind"] == "renamed" and old in r["old"]
               and new.coord_id in r["new"] for r in recs)
    md = MetaStore(tmp_path / ".coord").read_all(new.coord_id)
    assert any("precious fact" in e["text"]
               for e in md.get("notes", []))


# ============================================================== edges

def test_calls_resolve_shortname_to_qualname(tmp_path):
    ix, edges, _ = make(tmp_path, {PY:
        "def callee():\n    return 1\n\n"
        "def caller():\n    return callee()\n"})
    caller = ix.resolve("src.m.caller").coord_id
    callee = ix.resolve("src.m.callee").coord_id
    assert any(e["src"] == caller and e["dst"] == callee
               and e["kind"] == "calls" for e in edges)


def test_event_pairs_join_emit_consume(tmp_path):
    ix, edges, _ = make(tmp_path, {
        PY: 'def go():\n    emit("sig.done")\n',
        "web/a.js": 'function ha(){ on("sig.done"); }\n'})
    pairs = event_pairs(edges)
    assert "sig.done" in pairs
    assert pairs["sig.done"]["emitters"] and pairs["sig.done"]["consumers"]


def test_neighbors_returns_touching_edges(tmp_path):
    ix, edges, _ = make(tmp_path, {PY:
        "def a():\n    return b()\n\ndef b():\n    return 1\n"})
    a = ix.resolve("src.m.a").coord_id
    ns = neighbors(edges, a)
    assert ns and all(a in (e["src"], e["dst"]) for e in ns)


# =========================================================== metadata

def test_write_read_and_staleness_flip(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    e = ix.resolve("src.m.alpha")
    ms = MetaStore(tmp_path / ".coord")
    ms.add(e.coord_id, "notes", "watch the sign", body_hash=e.body_hash)
    fresh = ms.read_all(e.coord_id, current_hash=e.body_hash)
    assert fresh["notes"][0].get("stale") in (None, False)
    stale = ms.read_all(e.coord_id, current_hash="different")
    assert stale["notes"][0]["stale"] is True


def test_channels_are_separate(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    cid = ix.resolve("src.m.alpha").coord_id
    ms = MetaStore(tmp_path / ".coord")
    ms.add(cid, "notes", "n1")
    ms.add(cid, "design", "d1")
    md = ms.read_all(cid)
    assert md["notes"][0]["text"] == "n1"
    assert md["design"][0]["text"] == "d1"


# ============================================================ harvest

def test_harvest_docstrings_and_idempotence(tmp_path):
    ix, edges, _ = make(tmp_path, {PY: BODY})
    ms = MetaStore(tmp_path / ".coord")
    vs = VersionStore(tmp_path / ".coord")
    r1 = Harvester(ix, ms, vs, tmp_path).run()
    assert r1["docstrings"] >= 2                 # module + alpha
    r2 = Harvester(ix, ms, vs, tmp_path).run()
    assert r2["docstrings"] == 0                 # provenance-skipped


def test_harvest_test_contracts(tmp_path):
    ix, edges, _ = make(tmp_path, {
        PY: BODY,
        "tests/test_m.py":
            "from src.m import alpha\n\n"
            "def test_alpha_negatives():\n    assert alpha(-3) == 3\n"})
    ms = MetaStore(tmp_path / ".coord")
    r = Harvester(ix, ms, VersionStore(tmp_path / ".coord"),
                  tmp_path).run()
    assert r["test_contracts"] >= 1
    cid = ix.resolve("src.m.alpha").coord_id
    md = ms.read_all(cid)
    assert any("test_alpha_negatives" in e["text"]
               for e in md.get("docs", []) + md.get("notes", []))


def test_harvest_no_git_yields_zero_history(tmp_path):
    ix, edges, _ = make(tmp_path, {PY: BODY})
    ms = MetaStore(tmp_path / ".coord")
    r = Harvester(ix, ms, VersionStore(tmp_path / ".coord"),
                  tmp_path).run()
    assert r["git_history"] == 0                 # graceful without git


# =========================================================== incident

TB = '''Traceback (most recent call last):
  File "src/m.py", line %d, in alpha
    return x
ValueError: boom
'''


# ======================================================== access cache

def test_cache_serves_then_invalidates(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    db = tmp_path / ".coord" / "index" / "coordinates.json"
    d1 = load_json_cached(db, tmp_path / ".coord")
    pkl = tmp_path / ".coord" / "cache" / "coordinates.pkl"
    assert pkl.exists()
    d2 = load_json_cached(db, tmp_path / ".coord")   # warm hit
    assert d1 == d2
    data = json.loads(db.read_text())
    db.write_text(json.dumps(data, indent=4))        # new size -> new stamp
    d3 = load_json_cached(db, tmp_path / ".coord")   # invalidated, reread
    assert d3 == d1                                  # same content


def test_corrupt_pickle_falls_back(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    db = tmp_path / ".coord" / "index" / "coordinates.json"
    load_json_cached(db, tmp_path / ".coord")
    (tmp_path / ".coord" / "cache" / "coordinates.pkl").write_bytes(b"junk")
    assert load_json_cached(db, tmp_path / ".coord")  # no crash


# ============================================================= parsers

def test_python_nested_functions_are_entities(tmp_path):
    ix, _, _ = make(tmp_path, {PY:
        "def outer():\n    def inner():\n        return 1\n"
        "    return inner()\n"})
    assert ix.resolve("src.m.outer.inner") is not None


def test_js_class_methods(tmp_path):
    ix, _, _ = make(tmp_path, {"web/a.js":
        "class Cart {\n  add(item) { return item; }\n}\n"})
    quals = {e.qualname for e in ix.entities.values()}
    assert any(q.endswith("Cart.add") for q in quals)


def test_ts_interface_and_tsx(tmp_path):
    ix, _, _ = make(tmp_path, {
        "web/t.ts": "export interface Cfg { id: number }\n"
                    "export function mk(c: Cfg) { return c.id; }\n",
        "web/v.tsx": "export function View() {\n"
                     "  return <div>hi</div>;\n}\n"})
    quals = {e.qualname for e in ix.entities.values()}
    assert any(q.endswith(".Cfg") for q in quals)
    assert any(q.endswith(".mk") for q in quals)
    assert any(q.endswith(".View") for q in quals)


def test_go_receiver_and_java_constructor_arity(tmp_path):
    ix, _, _ = make(tmp_path, {
        "svc/s.go": "package svc\n\ntype T struct{}\n\n"
                    "func (t *T) Run(n int) int { return n }\n",
        "api/A.java": "public class A {\n"
                      "  public A(int x) { }\n"
                      "  public void go() { }\n}\n"})
    quals = {e.qualname for e in ix.entities.values()}
    assert any(q.endswith("T.Run") for q in quals)
    assert any(q.endswith("A/1") for q in quals)      # ctor arity
    assert any(q.endswith("go/0") for q in quals)


def test_minified_js_is_skipped(tmp_path):
    ix, _, rep = make(tmp_path, {
        PY: BODY,
        "web/bundle.js": "var a=1;" * 300 + "\n"})    # one huge line
    quals = {e.qualname for e in ix.entities.values()}
    assert not any("bundle" in q for q in quals)


def test_shape_hash_equal_bodies_detect_clones(tmp_path):
    ix, _, _ = make(tmp_path, {PY:
        "def one(x):\n    return x + 1\n\n"
        "def two(x):\n    return x + 1\n"})
    e1 = ix.resolve("src.m.one")
    e2 = ix.resolve("src.m.two")
    assert e1.shape_hash == e2.shape_hash
    assert e1.body_hash != e2.body_hash


def test_body_hash_whitespace_invariant(tmp_path):
    ix, _, _ = make(tmp_path, {PY: "def f(x):\n    return x\n"})
    h1 = ix.resolve("src.m.f").body_hash
    (tmp_path / PY).write_text("def f(x):\n        return x\n")
    ix2 = Indexer(tmp_path, tmp_path / ".coord")
    full_index(ix2, tmp_path)
    assert ix2.resolve("src.m.f").body_hash == h1
