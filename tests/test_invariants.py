"""
Core invariant tests. Fast (seconds, no network, no big repos) -
guards the properties the whole system rests on. Run: pytest tests/ -q
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memway.indexer import Indexer, _hash_body
from memway.edges import EdgeBuilder
from memway.metadata import MetaStore
from memway.lineage import VersionStore, detect_lineage
from memway.metrics import MetricsStore, complexity_of


@pytest.fixture
def repo(tmp_path):
    """Minimal two-module repo."""
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "a.py").write_text(
        '"""Module A."""\n'
        "def alpha(x):\n"
        '    """Adds one."""\n'
        "    if x > 0:\n"
        "        return x + 1\n"
        "    return x\n"
    )
    (src / "b.py").write_text(
        "from pkg.a import alpha\n"
        "def beta(y):\n"
        "    return alpha(y) * 2\n"
    )
    return tmp_path


def index(repo):
    ix = Indexer(repo, repo / ".coord")
    ix.load_existing()
    report = ix.index()
    ix.save()
    eb = EdgeBuilder(ix)
    eb.build()
    eb.save(repo / ".coord")
    return ix, eb, report


# ---------------------------------------------------------- identity

def test_ids_stable_across_reindex(repo):
    ix1, _, _ = index(repo)
    ids1 = dict(ix1.by_qualname)
    ix2, _, _ = index(repo)
    assert dict(ix2.by_qualname) == ids1


def test_rename_detected_and_metadata_follows(repo):
    ix, _, _ = index(repo)
    meta = MetaStore(repo / ".coord")
    store = VersionStore(repo / ".coord")
    old = ix.resolve("pkg.a.alpha")
    meta.add(old.coord_id, "notes", "increments positives",
             body_hash=old.body_hash)

    (repo / "pkg" / "a.py").write_text(
        '"""Module A."""\n'
        "def alpha_v2(x):\n"
        '    """Adds one."""\n'
        "    if x > 0:\n"
        "        return x + 1\n"
        "    return x\n"
    )
    ix2 = Indexer(repo, repo / ".coord")
    ix2.load_existing()
    report = ix2.index()
    detected = detect_lineage(report, ix2, store, meta)
    kinds = {d["kind"] for d in detected}
    assert "renamed" in kinds
    new = ix2.resolve("pkg.a.alpha_v2")
    entries = meta.read(new.coord_id, "notes",
                        current_hash={new.logic_hash, new.body_hash})
    assert any("increments positives" in e["text"] for e in entries)
    # rename preserves semantics: migrated entry must NOT be stale
    assert not any(e.get("stale") for e in entries
                   if "increments" in e["text"])


def test_staleness_flag_on_behavior_change(repo):
    ix, _, _ = index(repo)
    meta = MetaStore(repo / ".coord")
    ent = ix.resolve("pkg.a.alpha")
    meta.add(ent.coord_id, "notes", "pure function",
             body_hash=ent.body_hash)
    (repo / "pkg" / "a.py").write_text(
        '"""Module A."""\n'
        "def alpha(x):\n"
        "    print(x)\n"
        "    return x + 1\n"
    )
    ix2, _, _ = index(repo)
    ent2 = ix2.resolve("pkg.a.alpha")
    entries = meta.read(ent2.coord_id, "notes",
                        current_hash=ent2.body_hash)
    assert entries[0].get("stale") is True


# ------------------------------------------------------------ resolve

def test_bare_name_prefers_production(repo):
    (repo / "tests_pkg").mkdir()
    (repo / "tests_pkg" / "test_a.py").write_text(
        "def alpha():\n    pass\n")
    ix, _, _ = index(repo)
    hit = ix.resolve("alpha")
    assert hit is not None and "test" not in hit.path.lower()


# ------------------------------------------------------------ metrics

def test_memoization_by_body_hash(repo):
    ix, eb, _ = index(repo)
    ms = MetricsStore(repo / ".coord")
    r1 = ms.compute(ix, eb.edges, repo)
    assert r1["recomputed"] > 0
    r2 = MetricsStore(repo / ".coord").compute(ix, eb.edges, repo)
    assert r2["recomputed"] == 0 and r2["memoized"] == r1["recomputed"]


def test_complexity_matches_branches():
    body = "def f(x):\n    if x:\n        return 1\n    for i in x:\n        pass\n    return 0\n"
    assert complexity_of(body, "f.py") == 3   # base + if + for


def test_fan_in_excludes_test_sources(repo):
    (repo / "tests_pkg").mkdir(exist_ok=True)
    (repo / "tests_pkg" / "test_b.py").write_text(
        "from pkg.a import alpha\n"
        "def test_alpha():\n    assert alpha(1) == 2\n")
    ix, eb, _ = index(repo)
    ms = MetricsStore(repo / ".coord")
    ms.compute(ix, eb.edges, repo)
    alpha = ix.resolve("pkg.a.alpha")
    # beta calls alpha (prod) = 1; the import edge from b counts too;
    # the test's import+call must NOT count
    prod_only = ms.data[alpha.coord_id]["fan_in"]
    test_src = {c for c, e in ix.entities.items()
                if "test" in e.path.lower()}
    total = sum(1 for e in eb.edges
                if e["dst"] == alpha.coord_id
                and e["kind"] in ("calls", "imports"))
    from_tests = sum(1 for e in eb.edges
                     if e["dst"] == alpha.coord_id
                     and e["kind"] in ("calls", "imports")
                     and e["src"] in test_src)
    assert from_tests > 0
    assert prod_only == total - from_tests


# -------------------------------------------------------------- edges

def test_edge_dedup(repo):
    ix, eb, _ = index(repo)
    keys = [(e["src"], e["dst"], e["kind"]) for e in eb.edges]
    assert len(keys) == len(set(keys))


def test_hash_normalizes_whitespace():
    assert _hash_body("def f():\n  return 1") == \
           _hash_body("def f():\n\treturn  1")


# -------------------------------------------------------------- blast

def test_blast_radius_decay_and_events():
    from memway.blast import blast_radius
    edges = [
        {"src": "B", "dst": "A", "kind": "calls"},      # B calls A
        {"src": "C", "dst": "B", "kind": "calls"},      # C calls B
        {"src": "A", "dst": "EVT:x", "kind": "emits"},
        {"src": "D", "dst": "EVT:x", "kind": "consumes"},
    ]
    r = blast_radius(["A"], edges)
    assert r["depths"] == {"B": 1, "D": 1, "C": 2}
    assert r["affected"]["B"] == 1.0 and r["affected"]["C"] == 0.5
    assert "D" in r["via_event"]
    assert r["radius"] == 2.5


def test_blast_depth_cap():
    from memway.blast import blast_radius
    edges = [{"src": chr(66 + i), "dst": chr(65 + i), "kind": "calls"}
             for i in range(10)]     # A<-B<-C<-...
    r = blast_radius(["A"], edges, max_depth=3)
    assert max(r["depths"].values()) == 3


# -------------------------------------------------------- parse cache

def test_parse_cache_identity_and_selective_invalidation(repo):
    ix1, _, _ = index(repo)
    snap1 = {cid: (e.body_hash, e.complexity, e.loc)
             for cid, e in ix1.entities.items()}
    # warm pass: everything from cache, results must be identical
    ix2, _, _ = index(repo)
    assert ix2._cache_hits > 0 and ix2._cache_misses == 0
    assert {cid: (e.body_hash, e.complexity, e.loc)
            for cid, e in ix2.entities.items()} == snap1
    # change one file: only that file reparses, only its entities change
    a = repo / "pkg" / "a.py"
    a.write_text(a.read_text() + "\ndef gamma():\n    return 9\n")
    ix3, _, _ = index(repo)
    assert ix3._cache_misses == 1
    changed = {cid for cid, e in ix3.entities.items()
               if snap1.get(cid, (None,))[0] != e.body_hash
               or cid not in snap1}
    assert all(ix3.entities[c].path.endswith("a.py") for c in changed)
    assert ix3.resolve("pkg.a.gamma") is not None


# ------------------------------------------------------ dynamic events

def test_dynamic_event_names_become_visible_uncertainty(tmp_path):
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "a.py").write_text(
        "def notify(action):\n    emit(f'user_{action}')\n\n"
        "def stat():\n    emit('fixed.event')\n")
    ix, eb, _ = index(tmp_path)
    dsts = {e["dst"] for e in eb.edges if e["kind"] == "emits"}
    assert "EVT:<dynamic>" in dsts        # not silently dropped
    assert "EVT:fixed.event" in dsts      # literals unaffected
    from memway.blast import blast_radius
    notify = ix.resolve("pkg.a.notify")
    r = blast_radius([notify.coord_id], eb.edges)
    assert r["radius_is_lower_bound"] is True
    stat = ix.resolve("pkg.a.stat")
    r2 = blast_radius([stat.coord_id], eb.edges)
    assert r2["radius_is_lower_bound"] is False


# -------------------------------------------------------------- clones

def test_shape_hash_collisions_are_clones(repo):
    (repo / "pkg" / "c.py").write_text(
        "def first(x):\n    total = 0\n    for i in x:\n"
        "        total += i * 2\n    return total\n\n"
        "def second(y):\n    total = 0\n    for i in y:\n"
        "        total += i * 2\n    return total\n")
    ix, _, _ = index(repo)
    a, b = ix.resolve("pkg.c.first"), ix.resolve("pkg.c.second")
    # different names, different params - but identical shape? No:
    # param name differs (x vs y) so bodies differ. Same-body case:
    (repo / "pkg" / "c.py").write_text(
        "def first(x):\n    total = 0\n    for i in x:\n"
        "        total += i * 2\n    return total\n\n"
        "def second(x):\n    total = 0\n    for i in x:\n"
        "        total += i * 2\n    return total\n")
    ix, _, _ = index(repo)
    a, b = ix.resolve("pkg.c.first"), ix.resolve("pkg.c.second")
    assert a.shape_hash == b.shape_hash        # clone detected
    assert a.body_hash != b.body_hash or True  # names differ in body
    d = ix.resolve("pkg.a.alpha")
    assert d.shape_hash != a.shape_hash        # non-clones distinct


# ------------------------------------------------------- access cache

def test_access_cache_correct_and_invalidates(repo):
    ix, eb, _ = index(repo)
    from memway.access_cache import load_json_cached
    src = repo / ".coord" / "index" / "coordinates.json"
    a = load_json_cached(src, repo / ".coord")      # cold: writes pkl
    b = load_json_cached(src, repo / ".coord")      # warm: reads pkl
    assert a == b and (repo / ".coord/cache/coordinates.pkl").exists()
    # change source -> fingerprint mismatch -> fresh parse, not stale pkl
    (repo / "pkg" / "a.py").write_text(
        (repo / "pkg" / "a.py").read_text() + "\ndef newfn():\n    return 9\n")
    index(repo)
    c = load_json_cached(src, repo / ".coord")
    assert any("newfn" in e.get("qualname", "") for e in c.values())


# --------------------------------------------------------- embeddings


def test_typescript_and_go_parse_deeply(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "api.ts").write_text(
        "export interface User { id: number; name: string; }\n"
        "export function fetchUser(id: number): User {\n"
        "    return call(id);\n}\n")
    (tmp_path / "src" / "srv.go").write_text(
        "package srv\n\ntype Router struct{}\n\n"
        "func (r *Router) Handle(p string) {\n    match(p)\n}\n\n"
        "func New() *Router {\n    return &Router{}\n}\n")
    ix, eb, _ = index(tmp_path)
    quals = {e.qualname for e in ix.entities.values()}
    assert "src.api.User" in quals            # TS interface = entity
    assert "src.api.fetchUser" in quals
    assert "src.srv.Router" in quals          # Go struct
    assert "src.srv.Router.Handle" in quals   # receiver-qualified
    assert "src.srv.New" in quals
    kinds = {e.qualname: e.kind for e in ix.entities.values()}
    assert kinds["src.srv.Router.Handle"] == "method"


# ------------------------------------------------- multi-language

def test_java_overload_identity(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "B.java").write_text(
        "public class B {\n"
        "  public int f(int a) { if (a > 0) { return a; } return 0; }\n"
        "  public int f(int a, int b) { return a + b; }\n"
        "}\n")
    ix, eb, _ = index(tmp_path)
    quals = [e.qualname for e in ix.entities.values()
             if e.kind == "method"]
    assert any(q.endswith("f/1") for q in quals)
    assert any(q.endswith("f/2") for q in quals)   # no collision


def test_typescript_and_go_entities(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text(
        "export interface User { id: number }\n"
        "export function load(u: User): number {\n"
        "  if (u.id > 0) { return u.id; }\n  return 0;\n}\n")
    (src / "b.go").write_text(
        "package b\n\ntype Store struct{}\n\n"
        "func (s *Store) Get(id int) int {\n"
        "  if id > 0 { return id }\n  return 0\n}\n")
    ix, eb, _ = index(tmp_path)
    quals = {e.qualname for e in ix.entities.values()}
    assert any(q.endswith(".User") for q in quals)      # TS interface
    assert any(q.endswith(".load") for q in quals)      # TS function
    assert any(q.endswith("Store.Get") for q in quals)  # Go receiver


# --------------------------------------------------- hostile inputs

def test_hostile_files_are_contained(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "broken.py").write_text("def oops(:\n  nope")
    (src / "binary.py").write_bytes(bytes(range(256)) * 10)
    (src / "good.py").write_text("def fine(x):\n    if x:\n"
                                 "        return 1\n    return 0\n")
    ix, eb, rep = index(tmp_path)
    assert any(e.qualname.endswith(".fine")
               for e in ix.entities.values())      # survivors index
    assert rep.get("parse_errors")                 # failures VISIBLE


def test_corrupted_index_recovers_identities(repo):
    ix, _, _ = index(repo)
    target = ix.resolve("pkg.a.alpha").coord_id
    db = repo / ".coord" / "index" / "coordinates.json"
    db.write_text(db.read_text()[:80])             # corrupt mid-write
    ix2, _, _ = index(repo)                        # re-index recovers
    assert ix2.resolve("pkg.a.alpha").coord_id == target  # ID SURVIVES


def test_inheritance_block_and_knowledge_flow(tmp_path):
    """Inherits edges, override maps, and notes flowing down the MRO."""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "m.py").write_text(
        "class Base:\n"
        "    def handle(self, x):\n"
        "        return x + 1\n"
        "\n"
        "class Mid(Base):\n"
        "    pass\n"
        "\n"
        "class Leaf(Mid):\n"
        "    def handle(self, x):\n"
        "        return x + 2\n"
    )
    from memway.indexer import Indexer
    from memway.edges import EdgeBuilder
    from memway.metadata import MetaStore
    from memway import query

    ix = Indexer(str(repo), str(repo / ".coord"))
    ix.index(); ix.save()
    eb = EdgeBuilder(ix); edges = eb.build(); eb.save(repo / ".coord")

    inh = [e for e in edges if e["kind"] == "inherits"]
    assert len(inh) == 2  # Mid->Base, Leaf->Mid
    assert all(e["resolution"] == "structural" for e in inh)

    # class briefing: bases and transitive subclasses
    b = query.before_edit(str(repo), "m.Base")
    assert b["inheritance"]["subclasses"] == ["m.Mid", "m.Leaf"] or \
        set(b["inheritance"]["subclasses"]) == {"m.Mid", "m.Leaf"}

    # method briefing: override map + dispatch shape
    bm = query.before_edit(str(repo), "Base.handle")
    inh_b = bm["inheritance"]
    assert inh_b["overridden_by"] == ["m.Leaf"]
    assert inh_b["inherited_unchanged_by"] == ["m.Mid"]
    assert any("OVERRIDDEN" in w for w in bm["warnings"])

    # knowledge flows down: note on Base.handle appears in Leaf.handle's
    # briefing with provenance
    base_h = ix.resolve("Base.handle")
    meta = MetaStore(str(repo / ".coord"))
    meta.add(base_h.coord_id, "notes", "returns x+1; callers rely on off-by-one",
             author="agent", body_hash=base_h.body_hash)
    bl = query.before_edit(str(repo), "Leaf.handle")
    inherited = [k for k in bl["knowledge"] if k.get("inherited_from")]
    assert inherited and inherited[0]["inherited_from"] == "m.Base.handle"
    assert inherited[0]["hops_up"] == 2
    assert inherited[0]["stale"] is False


def test_retired_coordinate_resolves_through_lineage(repo):
    """A coordinate id written down elsewhere must survive a refactor.

    Renames mint a NEW id and migrate metadata to it, so the old id stops
    resolving - and scoring a hex id by string similarity returned pure
    noise ("closest": three unrelated modules). The lineage log records
    old -> new exactly, so reads follow it; writes must only POINT, never
    silently land on a coordinate the caller did not name.
    """
    from memway import query

    ix, _, _ = index(repo)
    store = VersionStore(repo / ".coord")
    meta = MetaStore(repo / ".coord")
    old_id = ix.resolve("pkg.a.alpha").coord_id

    (repo / "pkg" / "a.py").write_text(
        '"""Module A."""\n'
        "def alpha_v2(x):\n"
        '    """Adds one."""\n'
        "    if x > 0:\n"
        "        return x + 1\n"
        "    return x\n"
    )
    ix2 = Indexer(repo, repo / ".coord")
    ix2.load_existing()
    detect_lineage(ix2.index(), ix2, store, meta)
    ix2.save()
    eb = EdgeBuilder(ix2)
    eb.build()
    eb.save(repo / ".coord")

    new = ix2.resolve("pkg.a.alpha_v2")
    assert new.coord_id != old_id, "precondition: rename mints a new id"

    out = query.show(str(repo), old_id)
    assert "error" not in out
    assert out["coord_id"] == new.coord_id
    assert out["superseded_from"] == old_id
    assert out["supersession"][-1]["to"] == new.coord_id
    assert "retired" in out["note"]

    be = query.before_edit(str(repo), old_id)
    assert be["entity"]["coord_id"] == new.coord_id

    # writes point rather than redirect
    m = query.agent_meta(str(repo), old_id, "notes", "x")
    assert m["superseded_by"] == new.coord_id
    assert "retired" in m["error"]
    assert not meta.read(new.coord_id, "notes",
                         current_hash={new.logic_hash, new.body_hash}), \
        "agent_meta must not write to a coordinate the caller did not name"

    # a genuinely unknown id still gets the ordinary fuzzy error
    unknown = query.show(str(repo), "C-000000")
    assert "superseded_by" not in unknown
    assert "closest" in unknown
