"""Comment channel with rot detection; design-doc binding with drift flags."""
from coordsys.indexer import Indexer
from coordsys.edges import EdgeBuilder
from coordsys import query


V1 = '''def price(x):
    # TODO tighten rounding
    # tax model: flat multiplier
    total = x * 3
    return total + 1
'''


def _idx(repo):
    ix = Indexer(str(repo), str(repo / ".coord"))
    ix.load_existing(); ix.index(); ix.save()
    eb = EdgeBuilder(ix); eb.build(); eb.save(repo / ".coord")
    return ix


def test_comments_harvested_with_markers(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    (repo / "m.py").write_text(V1)
    _idx(repo)
    b = query.before_edit(str(repo), "price")
    assert b["comments"]["total"] == 2
    assert b["comments"]["markers"][0]["tag"] == "TODO"
    assert b["comments"]["rot"] is False


def test_comment_rot_fires_and_clears(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    (repo / "m.py").write_text(V1)
    _idx(repo)
    # logic changes, comments do not -> rot
    (repo / "m.py").write_text(V1.replace("x * 3", "x * 4"))
    _idx(repo)
    b = query.before_edit(str(repo), "price")
    assert b["comments"]["rot"] is True
    assert any("COMMENT ROT" in w for w in b["warnings"])
    # touching the comments clears it
    (repo / "m.py").write_text(
        V1.replace("x * 3", "x * 4").replace("flat multiplier",
                                             "flat multiplier, v2"))
    _idx(repo)
    b = query.before_edit(str(repo), "price")
    assert b["comments"]["rot"] is False


def test_design_doc_binding_and_drift(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    (repo / "m.py").write_text(V1)
    d = repo / "docs" / "design"; d.mkdir(parents=True)
    (d / "001-pricing.md").write_text(
        "# Pricing\\nThe `m.price` function implements the flat model.\\n")
    _idx(repo)
    b = query.before_edit(str(repo), "price")
    assert b["design_docs"] == [
        {"doc": "docs/design/001-pricing.md", "status": "fresh"}]
    # logic drifts past the doc
    (repo / "m.py").write_text(V1.replace("x * 3", "x * 9"))
    _idx(repo)
    b = query.before_edit(str(repo), "price")
    assert b["design_docs"][0]["status"] == "entity-changed-since-doc"
    assert any("GOVERNED BY" in w for w in b["warnings"])
