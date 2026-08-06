"""Logic-tier hashing: cosmetics never invalidate; behavior always does."""
from memway.indexer import Indexer, _logic_hash
from memway.edges import EdgeBuilder
from memway.metadata import MetaStore
from memway.lineage import detect_lineage, VersionStore
from memway import query


V1 = '''def price(x):
    """Compute the price."""
    total = x * 3
    return total + 1
'''
COSMETIC = '''def price(x):
    """Compute the price, with tax baked in."""
    # tax model: flat multiplier plus base fee
    total = x * 3
    return total + 1
'''
LOGIC = '''def price(x):
    """Compute the price."""
    total = x * 4
    return total + 1
'''


def test_logic_hash_ignores_cosmetics_catches_logic():
    a, b, c = _logic_hash(V1), _logic_hash(COSMETIC), _logic_hash(LOGIC)
    assert a == b          # docstring + comment: same logic
    assert a != c          # constant changed: different logic


def _make(tmp_path, src):
    repo = tmp_path / "r"
    repo.mkdir(exist_ok=True)
    (repo / "m.py").write_text(src)
    return repo


def test_note_survives_cosmetic_edit_flags_logic_edit(tmp_path):
    repo = _make(tmp_path, V1)
    ix = Indexer(str(repo), str(repo / ".coord")); ix.index(); ix.save()
    EdgeBuilder(ix).build()
    e = ix.resolve("price")
    query.agent_meta(str(repo), "price", "notes", "returns tax-inclusive total")

    (repo / "m.py").write_text(COSMETIC)
    ix2 = Indexer(str(repo), str(repo / ".coord"))
    ix2.load_existing(); ix2.index(); ix2.save()
    eb = EdgeBuilder(ix2); eb.build(); eb.save(repo / ".coord")
    b = query.before_edit(str(repo), "price")
    assert b["knowledge"][0]["stale"] is False        # comment edit: fresh
    assert not any("STALE" in w for w in b["warnings"])

    (repo / "m.py").write_text(LOGIC)
    ix3 = Indexer(str(repo), str(repo / ".coord"))
    ix3.load_existing(); ix3.index(); ix3.save()
    eb = EdgeBuilder(ix3); eb.build(); eb.save(repo / ".coord")
    b = query.before_edit(str(repo), "price")
    assert b["knowledge"][0]["stale"] is True         # behavior changed
    assert any("STALE" in w for w in b["warnings"])


def test_metrics_memoized_across_cosmetic_edit(tmp_path):
    repo = _make(tmp_path, V1)
    ix = Indexer(str(repo), str(repo / ".coord")); ix.index(); ix.save()
    from memway.metrics import MetricsStore
    eb = EdgeBuilder(ix); edges = eb.build()
    MetricsStore(repo / ".coord").compute(ix, edges, repo)    # prime cache
    (repo / "m.py").write_text(COSMETIC)
    ix2 = Indexer(str(repo), str(repo / ".coord"))
    ix2.load_existing(); ix2.index()
    rep = MetricsStore(repo / ".coord").compute(ix2, edges, repo)
    assert rep["memoized"] >= 1                       # logic unchanged: cached


def test_lineage_logic_pass_confirms_rename_with_doc_tweak(tmp_path):
    repo = _make(tmp_path, V1)
    ix = Indexer(str(repo), str(repo / ".coord")); ix.index(); ix.save()
    ls = VersionStore(str(repo / ".coord"))
    ms = MetaStore(str(repo / ".coord"))
    # rename + docstring change: body hash breaks, logic hash holds
    (repo / "m.py").write_text(COSMETIC.replace("def price", "def quote"))
    ix2 = Indexer(str(repo), str(repo / ".coord"))
    ix2.load_existing()
    report = ix2.index()
    detected = detect_lineage(report, ix2, ls, ms)
    renames = [d for d in detected if d["kind"] == "renamed"]
    assert renames and "identical logic" in renames[0]["note"]
    assert renames[0]["author"] == "auto"
