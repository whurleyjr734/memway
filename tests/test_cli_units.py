"""CLI UNITS: every command's function called in-process, so the
command layer, the system's entire user surface, is
unit-covered like everything beneath it. No subprocess excuses:
each cmd_* runs in this interpreter, output asserted via capsys,
error contracts asserted via SystemExit.
"""

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import cli


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "shop.py").write_text('''"""Shop."""

def price(x):
    """Compute price."""
    if x > 0:
        emit("priced.ok")
        return x * 2
    return 0

def price_copy(x):
    if x > 0:
        emit("priced.ok")
        return x * 2
    return 0

def fmt_a(v):
    label = "$"
    amount = str(v)
    joined = label + amount
    return joined

def fmt_b(v):
    label = "$"
    amount = str(v)
    joined = label + amount
    return joined


# TWO ENTITIES SHARING A SHORT NAME, on purpose. The ambiguous-ref test
# below skipped itself for want of one, so the branch that reports "N
# entities match" instead of "no entity matches" was never executed here
# - the exact branch whose absence cost 0.55.5 and 0.56.0. Deliberately
# a name no other test resolves, so `price` stays unambiguous for them.
class Aisle:
    def restock(self, n):
        return n + 1


class Depot:
    def restock(self, n):
        return n + 2
''')
    (tmp_path / "web" ).mkdir()
    (tmp_path / "web" / "ui.js").write_text(
        'function show(){ on("priced.ok"); return 1; }\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_shop.py").write_text(
        "from src.shop import price\n\n"
        "def test_price_positive():\n    assert price(2) == 4\n")
    cli.cmd_init(str(tmp_path))
    return tmp_path


def out(capsys):
    cap = capsys.readouterr()
    return cap.out + cap.err


# ------------------------------------------------------------ dispatch

def test_main_usage_and_dispatch(repo, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["memway"])
    with pytest.raises(SystemExit):
        cli.main()                                   # usage + exit 1
    assert "grep finds it" in out(capsys)
    monkeypatch.setattr(sys, "argv", ["memway", "nope", "x"])
    with pytest.raises(SystemExit):
        cli.main()                                   # unknown command
    monkeypatch.setattr(sys, "argv",
                        ["memway", "show", str(repo), "price"])
    cli.main()                                       # real dispatch
    assert "src.shop.price" in out(capsys)


# ------------------------------------------------------ index family

def test_init_reports_and_reindex_is_stable(repo, capsys):
    cli.cmd_index(str(repo))
    o = out(capsys)
    assert "files cached" in o and "memoized" in o   # warm, idempotent
    db = json.loads((repo / ".coord" / "index" /
                     "coordinates.json").read_text())
    assert any(e["qualname"] == "src.shop.price" for e in db.values())


def test_index_warns_on_unparseable(repo, capsys):
    (repo / "src" / "junk.py").write_bytes(bytes(range(256)) * 8)
    cli.cmd_index(str(repo))
    assert "unparseable" in out(capsys)


def test_load_guard_missing_index(tmp_path):
    with pytest.raises(SystemExit) as ei:
        cli.cmd_show(str(tmp_path / "nothing"), "x")
    assert "memway init" in str(ei.value)


def test_load_guard_corrupt_index_message(repo):
    db = repo / ".coord" / "index" / "coordinates.json"
    db.write_text("{ corrupt")
    # self-heal path: snapshots exist, so a read command RECOVERS
    cli.cmd_show(str(repo), "price")               # no SystemExit


# ------------------------------------------------------------ harvest

def test_harvest_then_zero(repo, capsys):
    cli.cmd_harvest(str(repo))
    first = out(capsys)
    assert "docstrings=" in first
    cli.cmd_harvest(str(repo))
    assert "docstrings=0" in out(capsys)


# --------------------------------------------------------------- show

def test_show_entity_and_unknown_ref(repo, capsys):
    cli.cmd_harvest(str(repo))
    capsys.readouterr()
    cli.cmd_show(str(repo), "price")
    o = out(capsys)
    assert "src.shop.price" in o and "calls" in o or "src.shop.price" in o
    # An unresolved ref now EXITS NONZERO (0.54.1). It returned 0, so a
    # script could not tell a miss from a hit. The old assertion here
    # ended in `or True`, which is to say it asserted nothing at all.
    with pytest.raises(SystemExit) as ex:
        cli.cmd_show(str(repo), "does_not_exist")
    assert ex.value.code == 1
    assert "no entity matches" in out(capsys)


# --------------------------------------------------------------- meta

def test_meta_add_and_bad_channel(repo, capsys):
    cli.cmd_meta(str(repo), "price", "notes", "vat included")
    cli.cmd_show(str(repo), "price")
    assert "vat included" in out(capsys)
    with pytest.raises(SystemExit) as ei:
        cli.cmd_meta(str(repo), "price", "gossip", "x")
    assert "unknown channel" in str(ei.value)


# ------------------------------------------------------------ lineage

def test_lineage_full_log_and_single(repo, capsys):
    p = repo / "src" / "shop.py"
    # price's name also appears in its docstring, so renaming it
    # changes the name-stripped shape (conservative by design).
    # price_copy's name exists only in its def line - clean rename.
    p.write_text(p.read_text().replace("def price_copy(",
                                       "def cost_copy("))
    cli.cmd_index(str(repo))
    capsys.readouterr()
    cli.cmd_lineage(str(repo))
    assert "renamed" in out(capsys)
    cli.cmd_lineage(str(repo), "cost_copy")
    assert "price_copy" in out(capsys)               # ancestry shows old


# -------------------------------------------- small branch closure

def test_meta_unknown_ref(repo, capsys):
    with pytest.raises(SystemExit) as ex:
        cli.cmd_meta(str(repo), "ghost_ref", "notes", "x")
    assert ex.value.code == 1
    o = out(capsys)
    assert "no entity matches" in o
    assert "closest:" in o, "an unresolved ref should still suggest candidates"


def test_meta_ambiguous_ref_names_the_candidates(repo, capsys):
    """The other branch: several entities match, and saying "no entity
    matches" there is a false negative that sends the caller to grep."""
    import json as _json
    from memway.indexer import Indexer
    ix = Indexer(repo, repo / ".coord")
    ix.load_existing(write_cache=False)
    tails = {}
    for q in ix.by_qualname:
        tails.setdefault(q.rsplit(".", 1)[-1], []).append(q)
    dupes = [t for t, qs in tails.items() if len(qs) > 1]
    if not dupes:
        pytest.skip("fixture has no ambiguous short name")
    with pytest.raises(SystemExit):
        cli.cmd_show(str(repo), dupes[0])
    o = out(capsys)
    assert "ambiguous" in o, o
    assert all(q in o for q in tails[dupes[0]]), o


def test_lineage_unknown_ref(repo, capsys):
    cli.cmd_lineage(str(repo), "totally_unknown_thing_xyz")
    o = out(capsys)
    assert "no lineage" in o or "no entity" in o


def test_harvest_dirty_tree_note_under_git(repo, capsys):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=repo)
    subprocess.run(["git", "add", "-A"], cwd=repo,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "x"], cwd=repo, capture_output=True)
    (repo / "src" / "shop.py").write_text(
        (repo / "src" / "shop.py").read_text() + "\n# dirty\n")
    cli.cmd_index(str(repo))
    assert "uncommitted changes" in out(capsys)


# ------------------------------------------- final branch closure

def test_corrupt_index_without_snapshots_exits_with_help(tmp_path,
                                                         capsys):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    return 1\n")
    cli.cmd_init(str(tmp_path))
    import shutil
    shutil.rmtree(tmp_path / ".coord" / "versions")
    (tmp_path / ".coord" / "index" /
     "coordinates.json").write_text("{ corrupt")
    capsys.readouterr()
    cli.cmd_lineage(str(tmp_path))           # heals to empty, no crash
    # ...and a repo whose index dir vanished entirely exits with help
    shutil.rmtree(tmp_path / ".coord" / "index")
    with pytest.raises(SystemExit):
        cli.cmd_show(str(tmp_path), "x")


def test_show_signature_line(repo, capsys):
    cli.cmd_show(str(repo), "price")
    assert "sig=" in out(capsys)


def test_meta_author_is_not_asserted_as_human(repo, monkeypatch):
    """The CLI must not claim human review it cannot verify.

    MetaStore.add defaults author to "human", and cmd_meta passed nothing,
    so every CLI write was stamped human - including five confirm entries
    in this repo written by an agent driving the CLI. A confirm is an
    attestation; who vouched is the entire content of it.
    """
    import json as _json
    from memway import query

    def authors(ref, channel):
        from memway.indexer import Indexer
        ix = Indexer(repo, repo / ".coord")
        ix.load_existing()
        cid = ix.resolve(ref).coord_id
        p = repo / ".coord" / "meta" / cid / f"{channel}.jsonl"
        return [_json.loads(l)["author"]
                for l in p.read_text().splitlines() if l.strip()]

    cli.cmd_meta(str(repo), "price", "notes", "default author")
    assert authors("price", "notes") == ["cli"], "must not be 'human'"

    cli.cmd_meta(str(repo), "price", "notes", "explicit", author="wdh")
    assert authors("price", "notes")[-1] == "wdh"

    # the flag travels through argv dispatch, and only applies to meta
    monkeypatch.setattr(
        sys, "argv",
        ["memway", "meta", str(repo), "price", "notes", "viaflag",
         "--author", "reviewer-x"])
    cli.main()
    assert authors("price", "notes")[-1] == "reviewer-x"

    monkeypatch.setattr(sys, "argv", ["memway", "show", str(repo),
                                      "price", "--author", "nope"])
    with pytest.raises(SystemExit):
        cli.main()

    # the MCP path keeps its own identity, unchanged
    query.agent_meta(str(repo), "price", "notes", "from mcp")
    assert authors("price", "notes")[-1] == "agent"
