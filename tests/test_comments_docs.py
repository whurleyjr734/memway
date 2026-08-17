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


def test_examples_dir_is_not_scanned_for_design_bindings(tmp_path):
    """docs/**/examples/** is documentation OF the tool, not design docs.

    A repo publishing a site from /docs accumulates such files. Binding
    them rewrites docbindings.json on every reindex, leaving a dirty map
    in a clean tree - which teaches people to ignore a dirty map, the
    opposite of what the map is for.
    """
    import json
    repo = tmp_path / "r"; repo.mkdir()
    (repo / "m.py").write_text(V1)

    real = repo / "docs" / "design"; real.mkdir(parents=True)
    (real / "001-pricing.md").write_text(
        "# Pricing\nThe `m.price` function implements the flat model.\n")
    # both shapes the fix must exclude
    ex = repo / "docs" / "examples"; ex.mkdir(parents=True)
    (ex / "README.md").write_text(
        "# Example\nWire it up around `m.price` like this.\n")
    nested = repo / "docs" / "guides" / "examples"; nested.mkdir(parents=True)
    (nested / "hooks.md").write_text(
        "# Hook example\nSee `m.price` for the shape.\n")

    _idx(repo)
    b = query.before_edit(str(repo), "price")   # triggers harvest_docs
    assert b["design_docs"] == [
        {"doc": "docs/design/001-pricing.md", "status": "fresh"}]
    bindings = json.loads((repo / ".coord" / "docbindings.json").read_text())
    bound = set(bindings)
    assert "docs/design/001-pricing.md" in bound, "real design docs still bind"
    assert not [p for p in bound if "examples" in p], f"examples bound: {bound}"

    # the reported symptom: a second reindex must not rewrite the file
    before = (repo / ".coord" / "docbindings.json").read_bytes()
    _idx(repo)
    query.before_edit(str(repo), "price")
    assert (repo / ".coord" / "docbindings.json").read_bytes() == before, \
        "reindex rewrote docbindings.json - the map dirties on every index"


# ------------------------------------------- modules are not rot-checkable

def test_a_module_never_carries_comment_rot(tmp_path):
    """0.56.1: the flag ends at the source, not at a display filter.

    A module docstring has no boundary a hash can respect. This repo's own
    indexer.py docstring claims things about its module surface, about
    behaviour inside its functions, AND about lineage.py - three scopes in
    one paragraph. Hashing the whole file re-flagged the module on every
    edit and no confirm could ever stick (14 permanent entries); hashing
    only the surface would be clearable but silently blind to the second
    kind of claim.

    So no module gets the flag, and the check is on the DATA: a display
    filter would leave dead flags in .coord for the next reader to trip on.
    """
    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text(
        '"""Module docstring that will not be touched."""\n'
        "# a module-level comment, also untouched\n"
        "VALUE = 1\n\n\n"
        "def alpha(x):\n"
        '    """Doc."""\n'
        "    t = 0\n"
        "    for i in x:\n"
        "        t += i\n"
        "    return t\n")
    _idx(r)

    # move logic everywhere, touch no comment anywhere
    (r / "m.py").write_text(
        '"""Module docstring that will not be touched."""\n'
        "# a module-level comment, also untouched\n"
        "VALUE = 2\n\n\n"
        "def alpha(x):\n"
        '    """Doc."""\n'
        "    return sum(x)\n")
    ix = _idx(r)

    from memway.query import attention
    mods = [e for e in ix.entities.values() if e.kind == "module"]
    assert mods, "fixture produced no module entity"
    assert not any(getattr(e, "comment_rot", False) for e in mods), (
        "a module carries comment_rot IN THE DATA - the exclusion must be "
        "at the computation, not a filter over the output")

    a = attention(str(r), limit=10000)
    byq = {e.qualname: e for e in ix.entities.values()}
    flagged_mods = [q for q in a["comment_rot"]
                    if q in byq and byq[q].kind == "module"]
    assert not flagged_mods, f"a module reached the queue: {flagged_mods}"


def test_function_rot_is_untouched_and_still_precise(tmp_path):
    """The other half, and the one that must not be traded away. A
    function's comments and its body share a scope, so the signal is
    exact - and 0.56.1 changes nothing about it."""
    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text(
        "def alpha(x):\n"
        '    """Sums by looping."""\n'
        "    # walks each element\n"
        "    t = 0\n"
        "    for i in x:\n"
        "        t += i\n"
        "    return t\n")
    _idx(r)

    (r / "m.py").write_text(
        "def alpha(x):\n"
        '    """Sums by looping."""\n'
        "    # walks each element\n"
        "    return sum(x)\n")
    ix = _idx(r)

    from memway.query import attention
    fn = next(e for e in ix.entities.values() if e.qualname.endswith("m.alpha"))
    assert getattr(fn, "comment_rot", False), (
        "the precise signal was traded away with the imprecise one - a "
        "function whose logic moved with its comments untouched must flag")

    a = attention(str(r), limit=10000)
    assert any(q.endswith("m.alpha") for q in a["comment_rot"]), a["comment_rot"]
    assert a["comment_rot_total"] >= 1
