"""Comment channel with rot detection; design-doc binding with drift flags."""
from memway.indexer import Indexer
from memway.edges import EdgeBuilder
from memway import query


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


def test_comment_rot_confirm_suppresses_and_restales(tmp_path):
    """Confirm channel: rot fires → confirm written → rot suppressed →
    logic changes again → old confirm stale, rot returns."""
    repo = tmp_path / "r"; repo.mkdir()
    (repo / "m.py").write_text(V1)
    ix = _idx(repo)
    # logic changes, comments do not -> rot fires
    (repo / "m.py").write_text(V1.replace("x * 3", "x * 4"))
    ix = _idx(repo)
    b = query.before_edit(str(repo), "price")
    assert b["comments"]["rot"] is True
    # write confirm at current logic_hash
    from memway.metadata import MetaStore
    meta = MetaStore(repo / ".coord")
    price_e = ix.resolve("m.price")
    meta.add(price_e.coord_id, "confirm", "comments reviewed, still accurate",
             body_hash=price_e.logic_hash)
    # rot now suppressed
    b = query.before_edit(str(repo), "price")
    assert b["comments"]["rot"] is False
    # attention queue also clean
    att = query.attention(str(repo))
    assert "m.price" not in att["comment_rot"]
    # another logic change -> confirm goes stale, rot returns
    (repo / "m.py").write_text(V1.replace("x * 3", "x * 5"))
    ix = _idx(repo)
    b = query.before_edit(str(repo), "price")
    assert b["comments"]["rot"] is True
    att = query.attention(str(repo))
    assert "m.price" in att["comment_rot"]


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
