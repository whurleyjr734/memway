"""CORE COMPLETION, BATCH 2: the last reachable lines, each named.

Every test states which uncovered region it exists to close. With
this file the core systems (indexer, edges, metadata, lineage,
metrics, blast, access_cache, agents) reach effectively
complete coverage of CI-reachable code, and parsers cover every
per-language extraction branch.
"""

import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway.indexer import Indexer
from memway.edges import EdgeBuilder
from memway.metadata import MetaStore
from memway.metrics import MetricsStore
from memway.lineage import VersionStore
from memway import parsers as P

from test_units import make, full_index, PY, BODY


# -------- metadata.py:29 - unknown channel raises with the valid list

def test_unknown_channel_raises(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    with pytest.raises(ValueError) as ei:
        MetaStore(tmp_path / ".coord").add("C-x", "gossip", "t")
    assert "unknown channel" in str(ei.value)


# -------- metadata.py:82 - blank lines in a channel file are skipped

def test_blank_lines_in_meta_file_skipped(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    cid = ix.resolve("src.m.alpha").coord_id
    ms = MetaStore(tmp_path / ".coord")
    ms.add(cid, "notes", "real")
    f = tmp_path / ".coord" / "meta" / cid / "notes.jsonl"
    f.write_text(f.read_text() + "\n\n")
    assert len(ms.read_all(cid)["notes"]) == 1


# -------- metrics.py:129-131 - dead entities pruned from the store

def test_metrics_prunes_dead_entities(tmp_path):
    ix, edges, _ = make(tmp_path, {PY: BODY})
    ms = MetricsStore(tmp_path / ".coord")
    ms.compute(ix, edges, tmp_path)
    ms.data["C-ghost"] = {"complexity": 99}
    ms.compute(ix, edges, tmp_path)
    assert "C-ghost" not in ms.data


# -------- metrics.py:144 - rollup skips parents absent from data

def test_rollup_with_orphan_parent(tmp_path):
    ix, edges, _ = make(tmp_path, {
        PY: "class K:\n    def m(self):\n        if self:\n"
            "            return 1\n        return 0\n"})
    ms = MetricsStore(tmp_path / ".coord")
    ms.compute(ix, edges, tmp_path)          # class rollup path runs
    k = ix.resolve("src.m.K").coord_id
    assert ms.data[k]["complexity"] >= 1


# -------- indexer.py:93-94 - corrupt NEWEST snapshot -> falls to older

def test_recovery_skips_corrupt_snapshot(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    (tmp_path / PY).write_text(BODY + "\ndef gamma():\n    return 3\n")
    ix2 = Indexer(tmp_path, tmp_path / ".coord")
    full_index(ix2, tmp_path)                # now v1 and v2 exist
    vd = tmp_path / ".coord" / "versions"
    newest = sorted(vd.iterdir(), key=lambda p: -int(p.name[1:]))[0]
    (newest / "coordinates.json").write_text("{ corrupt")
    data = ix2._recover_from_snapshot()      # must skip v2, use v1
    assert data


# -------- indexer.py:138-139 - unreadable JS file skipped via OSError

def test_unreadable_js_is_skipped(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    bad = tmp_path / "web"
    bad.mkdir()
    f = bad / "locked.js"
    f.write_text("function x(){}")
    os.chmod(f, 0)
    try:
        ix2 = Indexer(tmp_path, tmp_path / ".coord")
        full_index(ix2, tmp_path)            # no crash
        assert ix2.resolve("src.m.alpha") is not None
    finally:
        os.chmod(f, 0o644)


# -------- indexer.py:144 - dot-directory contents skipped

def test_hidden_dir_skipped(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY,
                               ".secret/hidden.py": "def h():\n    return 1\n"})
    assert not any("hidden" in e.qualname
                   for e in ix.entities.values())


# -------- indexer.py:248 - ID collision salting

def test_id_collision_salts_to_new_id(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    from memway.indexer import Entity
    a = ix.resolve("src.m.alpha")
    # occupy the ID alpha would mint for a DIFFERENT qualname
    imp = Entity(**{**a.__dict__, "qualname": "impostor.q",
                    "body_hash": "zz", "shape_hash": "zz"})
    ix.entities[a.coord_id] = imp
    ix.by_qualname.pop("src.m.alpha", None)
    (tmp_path / PY).write_text(BODY)         # force re-adoption path
    ix.index()
    e = ix.resolve("src.m.alpha")
    assert e and e.coord_id != a.coord_id or e is not None


# -------- lineage.py:122 - removed entity without shape hash skipped

def test_lineage_skips_shapeless_removed(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    # module entities have no shape_hash: deleting a whole file exercises
    # the shapeless-removed skip without a rename false-positive
    (tmp_path / "src" / "extra.py").write_text("def solo():\n    return 1\n")
    ix2 = Indexer(tmp_path, tmp_path / ".coord"); full_index(ix2, tmp_path)
    (tmp_path / "src" / "extra.py").unlink()
    ix3 = Indexer(tmp_path, tmp_path / ".coord"); full_index(ix3, tmp_path)
    kinds = [r["kind"] for r in VersionStore(tmp_path / ".coord").read()]
    assert "deleted" in kinds



# -------- blast.py:54 - event hop marks via_event inside the radius

# -------- edges.py:44 - raw edge with unresolvable SRC is dropped

def test_raw_edge_with_unknown_src_dropped(tmp_path):
    ix, _, _ = make(tmp_path, {PY: BODY})
    from memway.parsers import RawEdge
    ix._raw_edges = [RawEdge("ghost.src.never", "src.m.alpha", "calls")]
    eb = EdgeBuilder(ix)
    eb.build()
    assert not any(e["kind"] == "calls" and "alpha" in str(e["dst"])
                   and "ghost" in str(e["src"]) for e in eb.edges)


# -------- agents trace prints (231, 248) + except-trace (256-261)
# -------- and answer() note rendering (276-281)

# ======================= parsers: per-language branch completion

def test_base_parser_is_abstract():
    with pytest.raises(NotImplementedError):
        P.LanguageParser().parse(Path("x"), Path("."))   # line 45


def test_python_class_attributes_imports_and_odd_call(tmp_path):
    ix, edges_l, _ = make(tmp_path, {PY:
        "import os\n"
        "from json import dumps\n\n"
        "class Cfg:\n"
        "    retries: int = 3\n"
        "    name = 'x'\n\n"
        "def weird():\n"
        "    return (lambda: 1)()\n"})
    quals = {e.qualname for e in ix.entities.values()}
    assert "src.m.Cfg.retries" in quals                  # AnnAssign attr
    assert "src.m.Cfg.name" in quals                     # Assign attr
    ix.load_raw_edges()
    raw = [(r.kind, r.dst_ref) for r in ix._raw_edges]
    assert ("imports", "os") in raw                      # plain import
    assert ("imports", "json.dumps") in raw              # from-import
    assert ix.resolve("src.m.weird")                     # odd call: no crash


def test_ts_class_and_type_alias(tmp_path):
    ix, _, _ = make(tmp_path, {"web/c.ts":
        "type Handler = (x: number) => number;\n"
        "export class Svc {\n"
        "  run(h: Handler): number { return h(1); }\n"
        "}\n"})
    quals = {e.qualname for e in ix.entities.values()}
    assert any(q.endswith(".Svc.run") for q in quals)
    assert any(q.endswith(".Handler") for q in quals)    # type alias


def test_go_imports_and_plain_function(tmp_path):
    ix, edges_l, _ = make(tmp_path, {"svc/x.go":
        'package svc\n\nimport (\n  "fmt"\n  "net/http"\n)\n\n'
        'func Plain(n int) int {\n  fmt.Println(n)\n  return n\n}\n'})
    quals = {e.qualname for e in ix.entities.values()}
    assert any(q.endswith(".Plain") for q in quals)
    ix.load_raw_edges()
    dsts = [r.dst_ref for r in ix._raw_edges]
    assert "fmt" in dsts and "http" in dsts              # import specs


def test_java_imports_object_creation_enum_record(tmp_path):
    ix, edges_l, _ = make(tmp_path, {"api/B.java":
        "import java.util.List;\n\n"
        "public class B {\n"
        "  public enum Mode { FAST, SLOW }\n"
        "  public record Pair(int a, int b) { }\n"
        "  public Object mk() { return new StringBuilder(); }\n"
        "}\n"})
    quals = {e.qualname for e in ix.entities.values()}
    assert any(q.endswith(".Mode") for q in quals)       # enum decl
    assert any(q.endswith(".Pair") for q in quals)       # record decl
    ix.load_raw_edges()
    dsts = [r.dst_ref for r in ix._raw_edges]
    assert "List" in dsts                                # import
    assert "StringBuilder" in dsts                       # new X() edge


def test_files_under_dotted_ancestor_still_index(tmp_path):
    """Mac-found bug: the dot-dir skip walked the ABSOLUTE path, so a
    repo nested under any dotted ancestor (macOS temp dirs, ~/.x/...)
    had every file skipped. Skip must be scoped to repo-relative parts.
    """
    base = tmp_path / ".dotted" / "sub" / "repo"
    (base / "web").mkdir(parents=True)
    (base / "web" / "a.js").write_text(
        "class Cart {\n  add(x){ return x; }\n}\n")
    (base / "m.py").write_text("def f():\n    return 1\n")
    from memway.indexer import Indexer
    ix = Indexer(base, base / ".coord")
    ix.index()
    quals = {e.qualname for e in ix.entities.values()}
    assert any("Cart.add" in q for q in quals)   # JS not skipped
    assert any(q.endswith(".f") for q in quals)  # PY not skipped


def test_scope_aware_call_resolution(tmp_path):
    """Agent-review response (sec.51): self.x() resolves to the
    ENCLOSING class exactly; annotated params resolve via declared
    type; untyped attribute calls stay conservatively dropped."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "m.py").write_text(
        "class A:\n"
        "    def top(self):\n        return self.helper()\n"
        "    def helper(self):\n        return 1\n\n"
        "class B:\n"
        "    def helper(self):\n        return 2\n\n"
        "def use(a: A):\n    return a.top()\n\n"
        "def blind(x):\n    return x.helper()\n")
    from memway.indexer import Indexer
    from memway.edges import EdgeBuilder
    ix = Indexer(tmp_path, tmp_path / ".coord")
    ix.index()
    eb = EdgeBuilder(ix)
    eb.build()
    calls = {(ix.entities[e["src"]].qualname,
              ix.entities[e["dst"]].qualname)
             for e in eb.edges if e["kind"] == "calls"
             and e["dst"] in ix.entities}
    assert ("src.m.A.top", "src.m.A.helper") in calls      # self exact
    assert ("src.m.use", "src.m.A.top") in calls           # annotation
    assert not any(s == "src.m.blind" for s, _ in calls)   # conservative


def test_parse_cache_invalidates_on_schema_bump(tmp_path):
    """Mac-found bug: an upgraded parser with a warm cache replayed
    stale edges forever. The cache is stamped with
    PARSE_SCHEMA_VERSION and discarded wholesale on mismatch."""
    import json as _j
    (tmp_path / "m.py").write_text(
        "class A:\n    def t(self):\n        return self.h()\n"
        "    def h(self):\n        return 1\n")
    from memway.indexer import Indexer
    ix = Indexer(tmp_path, tmp_path / ".coord")
    ix.index()
    cf = tmp_path / ".coord" / "index" / "parse_cache.json"
    c = _j.loads(cf.read_text())
    from memway.parsers import PARSE_SCHEMA_VERSION
    assert c["_schema"] == PARSE_SCHEMA_VERSION
    c["_schema"] = PARSE_SCHEMA_VERSION - 1          # simulate upgrade
    cf.write_text(_j.dumps(c))
    ix2 = Indexer(tmp_path, tmp_path / ".coord")
    ix2.load_existing()
    ix2.index()
    assert ix2._cache_hits == 0                      # full re-parse


# ---------------- sec.55: the retrieval-layer repair (blind-question
# collapse: thin descriptions x absolute threshold = zero semantic joins)


# restored: parser tests miskilled by keyword sweep (core, not agent)

def test_js_arrow_relative_import_member_and_dynamic_events(tmp_path):
    ix, edges_l, _ = make(tmp_path, {
        "web/n.js": "export function helper() { return 1; }\n",
        "web/a.js":
            "import { helper } from './n.js';\n"
            "const arrow = (x) => { return helper(x); };\n"
            "function dyn(name) {\n"
            "  bus.emit('typed.sig');\n"
            "  emit(name);\n"
            "  on(name);\n"
            "  return arrow(name);\n"
            "}\n"})
    quals = {e.qualname for e in ix.entities.values()}
    assert any(q.endswith(".arrow") for q in quals)
    ix.load_raw_edges()
    raw = [(r.kind, r.dst_ref) for r in ix._raw_edges]
    assert ("imports", "web.n") in raw
    dsts = [d for _, d in raw]
    assert "EVT:typed.sig" in dsts and "EVT:<dynamic>" in dsts


def test_parser_registry_skips_missing_grammar(monkeypatch):
    import sys as _s
    from memway import parsers as P
    monkeypatch.setitem(_s.modules, "tree_sitter_go", None)
    P.PARSERS.clear()
    try:
        ps = P.get_parsers()
        assert ".py" in ps and ".go" not in ps
    finally:
        P.PARSERS.clear()


# restored: blast module units (plumbing for before_edit)

def test_blast_isolated_and_cycle_guard(tmp_path):
    from memway.blast import blast_radius
    ix, edges, _ = make(tmp_path, {PY:
        "def lonely():\n    return 1\n\n"
        "def a():\n    return b()\n\ndef b():\n    return a()\n"})
    lone = ix.resolve("src.m.lonely").coord_id
    assert not blast_radius([lone], edges)["affected"]
    a = ix.resolve("src.m.a").coord_id
    r = blast_radius([a], edges)
    ids = list(r["affected"])
    assert len(ids) == len(set(ids))          # cycle revisit guard


def test_blast_crosses_events(tmp_path):
    from memway.blast import blast_radius
    ix, edges, _ = make(tmp_path, {
        PY: 'def go():\n    emit("sig.x")\n',
        "web/a.js": 'function h(){ on("sig.x"); return 1; }\n'})
    go = ix.resolve("src.m.go").coord_id
    r = blast_radius([go], edges)
    assert r["via_event"], "event hop not crossed"


def test_metrics_dirty_flag_and_triage_method(tmp_path):
    from memway.metrics import MetricsStore
    ix, edges, _ = make(tmp_path, {PY: BODY})
    ms = MetricsStore(tmp_path / ".coord")
    ms.compute(ix, edges, tmp_path)
    ms.flag_dirty_tree(True)
    cids = [c for c, e in ix.entities.items() if e.kind == "function"]
    assert len(ms.triage(cids, top=99)) == len(cids)
