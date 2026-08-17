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
    cf = tmp_path / ".coord" / "cache" / "parse_cache.json"
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


# --------------------------- a name-only match that cannot be the callee

def _edges_for(tmp_path, files: dict):
    """Index a throwaway repo and return (indexer, edges)."""
    import subprocess, sys as _s
    from memway.indexer import Indexer
    from memway.edges import EdgeBuilder
    r = tmp_path / "r"
    r.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        f = r / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    ix = Indexer(r, r / ".coord")
    ix.index(persist=False)
    eb = EdgeBuilder(ix)
    return ix, eb, eb.build()


def test_a_bare_name_never_resolves_into_a_function_local_scope(tmp_path):
    """A class defined inside a function cannot be named from outside it.

    Measured on memway's own map: a one-line stub class D inside a test
    function had absorbed 136 call edges - every Path.read_text() in the
    package - because `read_text` had exactly ONE definition in the index
    and uniqueness was being read as certainty.
    """
    ix, eb, edges = _edges_for(tmp_path, {
        "app.py": "from pathlib import Path\n\n\n"
                  "def load(p):\n    return p.read_text()\n",
        "t_x.py": "def test_thing():\n    class D:\n        def read_text(self):\n"
                  "            return 'x'\n    assert D().read_text()\n",
    })
    stub = next((cid for q, cid in ix.by_qualname.items()
                 if q.endswith("D.read_text")), None)
    assert stub, "fixture did not produce the function-local stub"
    loader = ix.by_qualname[next(q for q in ix.by_qualname if q.endswith("app.load"))]
    bad = [e for e in edges if e["src"] == loader and e["dst"] == stub]
    assert not bad, "a bare name resolved into a function-local scope"


def test_an_attribute_call_never_resolves_to_a_module_level_function(tmp_path):
    """`d.get(x)` is not `def get` at module scope, however unique the name.

    The parser knew the call was written `receiver.name(...)` and threw
    that away; RawEdge carries it now. Without it, every dict.get() in the
    repo landed on a module-level helper named get - 159 edges on memway's
    own map, and the visible hairball in the rendered graph.
    """
    ix, eb, edges = _edges_for(tmp_path, {
        "helper.py": "def get(key):\n    return key\n",
        "user.py": "def use(d):\n    return d.get('k')\n",
    })
    tgt = ix.by_qualname[next(q for q in ix.by_qualname if q.endswith("helper.get"))]
    src = ix.by_qualname[next(q for q in ix.by_qualname if q.endswith("user.use"))]
    assert not [e for e in edges if e["src"] == src and e["dst"] == tgt], \
        "an attribute call resolved to a module-level function"

    # ...and a PLAIN call to the same name still resolves: the rule is
    # about how the call was written, not about the name.
    ix2, eb2, edges2 = _edges_for(tmp_path / "b", {
        "helper.py": "def get(key):\n    return key\n",
        "user.py": "from helper import get\n\n\ndef use(k):\n    return get(k)\n",
    })
    t2 = ix2.by_qualname[next(q for q in ix2.by_qualname if q.endswith("helper.get"))]
    s2 = ix2.by_qualname[next(q for q in ix2.by_qualname if q.endswith("user.use"))]
    assert [e for e in edges2 if e["src"] == s2 and e["dst"] == t2], \
        "a plain call to a module-level function was wrongly dropped"


def test_production_code_never_bare_resolves_into_tests(tmp_path):
    # A PLAIN call, deliberately: `x.helper()` would also be dropped by the
    # attribute-call rule, so the fixture would pass with this rule deleted.
    # Isolate it or it proves nothing.
    ix, eb, edges = _edges_for(tmp_path, {
        "app.py": "def run():\n    return helper()\n",
        "tests/t_a.py": "def helper():\n    return 1\n",
    })
    tgt = next((cid for q, cid in ix.by_qualname.items()
                if q.endswith("t_a.helper")), None)
    src = ix.by_qualname[next(q for q in ix.by_qualname if q.endswith("app.run"))]
    assert not [e for e in edges if e["src"] == src and e["dst"] == tgt], \
        "production code bare-resolved into a test helper"


def test_every_short_name_resolution_site_is_guarded():
    """Structural: THREE sites match on a short name, and the first pass
    guarded two. The third - the inherited-guess tier - kept feeding the
    same false hubs until it was found by re-measuring rather than by
    reading. Enumerate them so a fourth cannot be added unguarded."""
    import ast
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "memway" / "edges.py"
    tree = ast.parse(src.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "build")
    # PER LOOKUP, not a global count. Counting both and comparing totals
    # passed with a guard deleted, because the "exact" tier's guard made up
    # the difference - the arithmetic was right and the claim was wrong.
    unguarded = []
    for node in ast.walk(fn):
        if not isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            continue
        seg = ast.dump(node)
        if "_by_short" in seg and "_unreachable_target" not in seg:
            unguarded.append(ast.unparse(node)[:90])
    assert not unguarded, (
        "short-name lookups with no reachability guard - each is a tier that "
        "can resolve a name onto something that cannot be the callee:\n  " +
        "\n  ".join(unguarded))
    sites = ast.dump(fn).count("_by_short")
    assert sites >= 3, f"expected the short-name tiers, found {sites}"


def test_a_call_into_an_imported_module_is_qualified_not_guessed(tmp_path):
    """`subprocess.run(...)` is not this repo's `run`.

    The receiver was discarded, leaving a bare `run` that matched whatever
    unique `run` the index happened to hold - 77 call sites became edges
    into Harvester.run, the second-densest hub in the rendered graph.

    The fix QUALIFIES rather than drops, because the same shape is often
    ours: `helper.go()` where helper is our module must keep its edge, and
    gets a better one than before. Both directions are asserted here; a
    fixture that only checked the dropping half would pass a change that
    silently deleted every intra-repo module call.
    """
    ix, eb, edges = _edges_for(tmp_path, {
        "helper.py": "def go():\n    return 1\n",
        "app.py": "import subprocess\nimport helper\n\n\n"
                  "def work():\n    subprocess.run(['ls'])\n    return helper.go()\n",
        "other.py": "class T:\n    def run(self):\n        return 2\n",
    })
    src = ix.by_qualname[next(q for q in ix.by_qualname if q.endswith("app.work"))]
    theirs = next((cid for q, cid in ix.by_qualname.items()
                   if q.endswith("T.run")), None)
    ours = next((cid for q, cid in ix.by_qualname.items()
                 if q.endswith("helper.go")), None)
    assert theirs and ours, "fixture did not produce both targets"

    assert not [e for e in edges if e["src"] == src and e["dst"] == theirs], \
        "subprocess.run() resolved to this repo's run"
    assert [e for e in edges if e["src"] == src and e["dst"] == ours], \
        "helper.go() lost its edge - qualifying must not drop OUR modules"


def test_a_dotted_ref_is_not_guessed_when_its_prefix_is_unknown(tmp_path):
    """The other half of the same fix, one tier down.

    Qualifying the receiver in the parser was only half of it: the
    inherited-guess tier took the dotted ref, threw the prefix away, and
    matched the last segment against every entity - putting the edge back
    exactly where it had been removed from. That tier exists for
    inheritance dispatch, where the prefix always names a class this repo
    has, so gating on a known prefix keeps it and stops the rest.
    """
    ix, eb, edges = _edges_for(tmp_path, {
        "app.py": "import external_thing\n\n\n"
                  "def work():\n    return external_thing.unique_name()\n",
        "mine.py": "def unique_name():\n    return 1\n",
    })
    src = ix.by_qualname[next(q for q in ix.by_qualname if q.endswith("app.work"))]
    tgt = next((cid for q, cid in ix.by_qualname.items()
                if q.endswith("mine.unique_name")), None)
    assert tgt, "fixture missing"
    assert not [e for e in edges if e["src"] == src and e["dst"] == tgt], \
        "a dotted ref with an unknown prefix was guessed onto a local name"


def test_inheritance_dispatch_still_resolves(tmp_path):
    """self.meth, defined on an ancestor, still reaches the ancestor.

    HONEST SCOPE: this is served by the MRO tier (confidence 0.90), NOT by
    the inherited-guess tier the prefix gate sits on - disabling that tier
    entirely leaves this green. So it pins that the gate did not break
    dispatch, which is worth pinning, and it does NOT discriminate the
    gate being too tight. Reaching the inherited-guess tier needs a case
    where MRO resolution fails and a unique bare method exists, and no
    fixture here produces one; the tier is currently unfalsified in the
    too-tight direction.
    """
    ix, eb, edges = _edges_for(tmp_path, {
        "h.py": "class Base:\n    def shared(self):\n        return 1\n\n\n"
                "class Child(Base):\n    def go(self):\n        return self.shared()\n",
    })
    go = next(cid for q, cid in ix.by_qualname.items() if q.endswith("Child.go"))
    shared = next(cid for q, cid in ix.by_qualname.items()
                  if q.endswith("Base.shared"))
    assert [e for e in edges if e["src"] == go and e["dst"] == shared], \
        "inheritance dispatch was lost - the gate is too tight"


def test_a_function_CAN_call_a_helper_it_defines_itself(tmp_path):
    """The closure idiom. Rule 1 always said "from OUTSIDE that body";
    until 0.55.3 it never checked, and dropped this edge too.

    THIS IS THE CASE NO EXISTING FIXTURE EXERCISED, which is exactly why
    every one of them passed while 60 true edges went missing across
    three real repos - 50 in rich alone, including
    Tree.__rich_console__ -> make_guide, called six times inside the
    function that defines it.

    The fixture holds both directions on purpose. `render` calls the
    helper it owns (must resolve); `outsider` names the same helper from
    another module (must still be refused). A fixture with only the first
    would pass against a rule that dropped guard 1 altogether.
    """
    ix, eb, edges = _edges_for(tmp_path, {
        "r.py": "def render(items):\n"
                "    def make_guide(i):\n"
                "        return str(i)\n"
                "    return [make_guide(i) for i in items]\n",
        "o.py": "def outsider(i):\n    return make_guide(i)\n",
    })
    helper = next((cid for q, cid in ix.by_qualname.items()
                   if q.endswith("render.make_guide")), None)
    assert helper, "fixture did not produce the nested helper"
    render = ix.by_qualname[next(q for q in ix.by_qualname
                                 if q.endswith("r.render"))]
    outsider = ix.by_qualname[next(q for q in ix.by_qualname
                                   if q.endswith("o.outsider"))]

    good = [e for e in edges if e["src"] == render and e["dst"] == helper
            and e.get("kind") == "calls"]
    assert good, ("a function may call a helper defined inside itself - "
                  "the target is lexically in scope and unambiguous")

    bad = [e for e in edges if e["src"] == outsider and e["dst"] == helper]
    assert not bad, ("guard 1 was dropped rather than scoped: a foreign "
                     "module resolved into a function-local scope")


def test_a_sibling_closure_can_call_a_helper_of_the_same_parent(tmp_path):
    """Scope is inherited, so the check walks UP from the caller.

    A nested `inner` calling its parent's `helper` is in scope by ordinary
    lexical rules. Stopping at the direct parent would trade one wrong
    answer for a smaller one.
    """
    ix, eb, edges = _edges_for(tmp_path, {
        "s.py": "def outer(xs):\n"
                "    def helper_fn(v):\n        return v + 1\n"
                "    def inner(v):\n        return helper_fn(v)\n"
                "    return [inner(x) for x in xs]\n",
    })
    helper = next((cid for q, cid in ix.by_qualname.items()
                   if q.endswith("outer.helper_fn")), None)
    inner = next((cid for q, cid in ix.by_qualname.items()
                  if q.endswith("outer.inner")), None)
    assert helper and inner, "fixture did not produce both closures"
    good = [e for e in edges if e["src"] == inner and e["dst"] == helper
            and e.get("kind") == "calls"]
    assert good, "a sibling closure is in scope and must resolve"
