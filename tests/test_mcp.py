"""Tests for the structured query core and the MCP server protocol -
the agent-facing surface. The MCP handshake and tool dispatch are
tested in-process (real JSON-RPC messages through handle()); the live
IDE wiring is the owner's machine test.
"""

import io
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import query, mcp, cli



@pytest.fixture()
def built(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "auth.py").write_text('''"""Auth."""

def sign(request, key):
    """Attach credentials to an outgoing request."""
    if key:
        request.headers["Authorization"] = key
        emit("signed.ok")
        return request
    return None
''')
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "ui.js").write_text(
        'function onSigned(){ on("signed.ok"); }\n')
    cli.cmd_init(str(tmp_path))
    cli.cmd_harvest(str(tmp_path))
    return str(tmp_path)


# ------------------------------------------------------------- query core

def test_summary_shape(built):
    s = query.summary(built)
    assert s["entities"] > 0 and s["edges"] >= 0
    assert ".py" in s["languages"]
    assert s["hardest"] and "qualname" in s["hardest"][0]


def test_show_returns_edges_and_knowledge(built):
    d = query.show(built, "sign")
    assert d["qualname"].endswith(".sign")
    assert d["signature"].startswith("sign(")
    assert any(k["channel"] == "docs" for k in d["knowledge"])
    assert isinstance(d["edges"], list)


def test_show_unknown_ref_is_error_not_exception(built):
    d = query.show(built, "does_not_exist")
    assert "error" in d


def test_hybrid_ref_resolution(tmp_path):
    """Hybrid refs: basic resolution, exact beats suffix, shortest wins ties."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    # Basic hybrid ref test: simple function
    (tmp_path / "src" / "auth.py").write_text('''"""Auth."""

def sign(request, key):
    """Attach credentials to an outgoing request."""
    return request
''')
    # File with both a function "check" and a method "Validator.check"
    (tmp_path / "src" / "validator.py").write_text('''
def check(value):
    """Top-level check function."""
    return True

class Validator:
    def check(self, value):
        """Method check."""
        return False
''')
    # File with nested classes, all having a "run" method
    (tmp_path / "src" / "runner.py").write_text('''
class Task:
    def run(self):
        pass

class Job:
    class Inner:
        def run(self):
            pass
''')
    cli.cmd_init(str(tmp_path))

    # Basic: "auth.py:sign" resolves
    d = query.show(str(tmp_path), "auth.py:sign")
    assert "error" not in d
    assert d["qualname"].endswith(".sign")
    # Also test with path prefix
    d2 = query.show(str(tmp_path), "src/auth.py:sign")
    assert "error" not in d2
    assert d2["qualname"].endswith(".sign")

    # Exact beats suffix: "validator.py:check" should resolve to the shorter
    # "src.validator.check" (exact function) not "src.validator.Validator.check"
    d3 = query.show(str(tmp_path), "validator.py:check")
    assert "error" not in d3
    assert d3["qualname"] == "src.validator.check"
    assert d3["kind"] == "function"

    # Deterministic shortest wins: both "src.runner.Task.run" and
    # "src.runner.Job.Inner.run" match "run" exactly; shortest wins
    d4 = query.show(str(tmp_path), "runner.py:run")
    assert "error" not in d4
    assert d4["qualname"] == "src.runner.Task.run"


def test_error_payload_contains_closest_and_hint(built):
    """When ref doesn't resolve, error should include fuzzy matches and hint."""
    d = query.show(built, "does_not_exist")
    assert "error" in d
    assert "closest" in d
    assert isinstance(d["closest"], list)
    assert len(d["closest"]) == 3
    assert "hint" in d
    assert "memway_at" in d["hint"]


def test_query_on_missing_index_returns_error(tmp_path):
    assert "error" in query.summary(str(tmp_path / "nope"))


# ----------------------------------------------------------- MCP protocol

def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method,
            "params": params or {}}


def test_initialize_handshake(built):
    r = mcp.handle(rpc("initialize"), built)
    assert r["result"]["serverInfo"]["name"] == "memway"
    assert "protocolVersion" in r["result"]


def test_initialized_notification_returns_none(built):
    assert mcp.handle(rpc("notifications/initialized", id_=None),
                      built) is None


def test_tools_list_advertises_every_tool(built):
    r = mcp.handle(rpc("tools/list"), built)
    names = {t["name"] for t in r["result"]["tools"]}
    assert names == {"memway_summary", "memway_at",
                     "memway_dig",
                     "memway_verify_change", "memway_probe", "memway_meta",
                     "memway_attention",
                     "memway_show", "memway_lineage",
                     "memway_before_edit"}
    for t in r["result"]["tools"]:                  # schema well-formed
        assert t["inputSchema"]["type"] == "object"


def test_tools_call_summary(built):
    r = mcp.handle(rpc("tools/call", {"name": "memway_summary",
                                      "arguments": {}}), built)
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["entities"] > 0


def test_tools_call_unknown_tool_errors(built):
    r = mcp.handle(rpc("tools/call", {"name": "nope", "arguments": {}}),
                   built)
    assert "error" in r


def test_tools_call_bad_args_returns_iserror_not_crash(built):
    r = mcp.handle(rpc("tools/call", {"name": "memway_show",
                                      "arguments": {}}), built)
    # missing required 'ref' -> handled as tool error, agent stays alive
    assert r["result"]["isError"] or "error" in \
        json.loads(r["result"]["content"][0]["text"])


def test_unknown_method_errors(built):
    r = mcp.handle(rpc("bogus/method"), built)
    assert r["error"]["code"] == -32601


def test_serve_loop_processes_lines(built):
    inp = io.StringIO(
        json.dumps(rpc("initialize")) + "\n"
        + json.dumps(rpc("tools/list", id_=2)) + "\n")
    outp = io.StringIO()
    mcp.serve(built, stdin=inp, stdout=outp)
    lines = [json.loads(l) for l in outp.getvalue().splitlines() if l]
    assert lines[0]["result"]["serverInfo"]["name"] == "memway"
    assert len(lines[1]["result"]["tools"]) == 10


# ------------------------------------------------------- before_edit

def test_before_edit_briefing_shape_and_warnings(built):
    from memway import cli as _cli
    # make it warned-about: stale note + a caller
    _cli.cmd_meta(built, "sign", "notes", "auth team owns this")
    p = Path(built) / "src" / "auth.py"
    p.write_text(p.read_text() + "\ndef caller(r):\n    return sign(r, 'k')\n")
    _cli.cmd_index(built)
    p.write_text(p.read_text().replace('request.headers["Authorization"] = key',
                                       'request.headers["Auth"] = key'))
    _cli.cmd_index(built)                        # note goes stale
    d = query.before_edit(built, "sign")
    assert d["entity"]["qualname"].endswith(".sign")
    assert d["metrics"]["complexity"] >= 1
    assert any(c["qualname"].endswith(".caller")
               for c in d["direct_callers"])
    assert "downstream_count" in d["downstream"]
    assert any(k["stale"] for k in d["knowledge"])
    assert any("STALE KNOWLEDGE" in w for w in d["warnings"])


def test_before_edit_unknown_ref(built):
    assert "error" in query.before_edit(built, "ghost")


def test_mcp_before_edit_tool(built):
    r = mcp.handle(rpc("tools/call", {"name": "memway_before_edit",
                                      "arguments": {"ref": "sign"}}),
                   built)
    payload = json.loads(r["result"]["content"][0]["text"])
    assert "warnings" in payload and "downstream" in payload


def test_summary_knowledge_census(built):
    """The census: summary answers "what does the map remember" -
    counts by channel, per-coordinate entries, freshness judged like
    before_edit. Uses the confirm channel when the tree has it."""
    from memway.metadata import CHANNELS
    ch2 = "confirm" if "confirm" in CHANNELS else "docs"

    r1 = query.agent_meta(built, "sign", "notes",
                          "margin is load-bearing; see D7")
    assert "attached" in r1
    r2 = query.agent_meta(built, "src.auth", ch2,
                          "reviewed after harvest; behavior as documented")
    assert "attached" in r2

    k = query.summary(built)["knowledge"]
    # harvest also mines docs entries, so assert >=, then pin OUR two
    assert k["total_entries"] >= 2
    assert k["by_channel"].get("notes", 0) >= 1
    assert k["by_channel"].get(ch2, 0) >= 1
    by_q = {e["qualname"]: e for e in k["entries"]}
    assert "src.auth.sign" in by_q and "notes" in by_q["src.auth.sign"]["channels"]
    assert "src.auth" in by_q and ch2 in by_q["src.auth"]["channels"]
    assert by_q["src.auth.sign"]["any_stale"] is False
    assert k["coordinates_with_knowledge"] == len(
        {e["coordinate"] for e in k["entries"]})

    # change sign's LOGIC -> its note must go stale in the census
    p = Path(built) / "src" / "auth.py"
    p.write_text(p.read_text().replace('emit("signed.ok")',
                                       'emit("signed.v2")'))
    cli.cmd_index(built)
    k2 = query.summary(built)["knowledge"]
    by_q2 = {e["qualname"]: e for e in k2["entries"]}
    assert by_q2["src.auth.sign"]["any_stale"] is True
    # stale coordinates sort first: the queue surfaces problems on top
    assert k2["entries"][0]["any_stale"] is True


def test_census_superseded_vs_orphaned(built):
    """Census distinguishes superseded (migrated) from orphaned (lost)
    knowledge. A rename mints new coordinates and migrates metadata - the
    old coordinate becomes superseded and must NOT inflate totals."""
    # Attach knowledge to an entity
    query.agent_meta(built, "sign", "notes", "test note on sign")
    k1 = query.summary(built)["knowledge"]
    initial_total = k1["total_entries"]
    initial_notes = k1["by_channel"].get("notes", 0)

    # Rename the entity (change function name in source)
    p = Path(built) / "src" / "auth.py"
    src = p.read_text()
    src = src.replace("def sign(", "def sign_renamed(")
    p.write_text(src)
    cli.cmd_index(built)

    # After rename: new coordinate with migrated knowledge + history receipt
    k2 = query.summary(built)["knowledge"]
    # Total should be initial + 1 history entry, NOT doubled
    assert k2["total_entries"] == initial_total + 1
    # Notes count unchanged (the original note, not duplicated)
    assert k2["by_channel"].get("notes", 0) == initial_notes
    # Should have 1 history entry (the migration receipt)
    assert k2["by_channel"].get("history", 0) == 1
    # Should report 1 superseded coordinate
    assert k2["superseded"] == 1
    # The old coordinate should NOT appear in the entries list
    coords = [e["coordinate"] for e in k2["entries"]]
    by_q = {e["qualname"]: e for e in k2["entries"] if e["qualname"]}
    # New qualname should appear
    assert "src.auth.sign_renamed" in by_q
    # Old qualname should NOT appear
    assert "src.auth.sign" not in by_q


def test_flight_recorder_logs_tool_calls(built):
    """Every MCP tool call leaves one line in .coord/log/usage.jsonl:
    tool name, entity ref, ok flag, shared session id. Local-only,
    reference-only (never payload text), and a failed lookup is
    recorded ok=false - which tools fail is itself a pattern."""
    def call(name, arguments):
        return mcp.handle({"jsonrpc": "2.0", "id": 1,
                           "method": "tools/call",
                           "params": {"name": name,
                                      "arguments": arguments}}, built)

    call("memway_summary", {})
    call("memway_show", {"ref": "sign"})
    call("memway_show", {"ref": "no_such_entity_xyz"})

    log = Path(built) / ".coord" / "log" / "usage.jsonl"
    assert log.exists()
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    assert [l["tool"] for l in lines] == [
        "memway_summary", "memway_show", "memway_show"]
    assert lines[1]["ref"] == "sign"
    assert [l["ok"] for l in lines] == [True, True, False]
    assert len({l["session"] for l in lines}) == 1
    # reference-only contract: no payload text fields ever
    assert all(set(l) <= {"ts", "session", "tool", "ref", "ok"}
               for l in lines)


def test_the_briefing_is_ranked_bounded_and_says_so(tmp_path):
    """A briefing is not a dump, and a truncated list must admit it.

    before_edit listed EVERY caller. On prometheus that made one pre-edit
    check 53,534 characters - roughly 13k tokens - of which 36,677 was a
    342-entry caller list and 15,947 was `downstream.direct` repeating the
    same 342 qualnames at 100% overlap. For an agent, which is what this
    surface is for, that is the density problem in its most expensive
    form: a context budget, not visual clutter.

    Three properties, and the third is the one this project cares about:
    the list is RANKED so the first entries are the useful ones, BOUNDED
    so a hot entity cannot blow the budget, and the truncation is VISIBLE.
    The guard message elsewhere in this codebase reads "nothing is ever
    sampled silently", and a list that quietly stops at twelve is a
    sampled list.
    """
    import subprocess as sp
    # TWO FILES, because is_test_entity classifies by PATH, not by
    # function name - naming a function test_* inside m.py fooled the
    # first version of this fixture and it asserted on zero test callers.
    body = ["class Hub:", "    def target(self):", "        return 1", ""]
    for i in range(20):                       # 20 production callers
        body += [f"def worker_{i}(h):", "    return h.target()", ""]
    # FILE NAMES CHOSEN SO THE UNSORTED ORDER PUTS TESTS FIRST. With
    # m.py / test_m.py the natural edge order already happened to be
    # production-first, so deleting the sort changed nothing and the
    # ranking assertion passed on luck - caught by falsifying it.
    (tmp_path / "zz_prod.py").write_text("\n".join(body))
    tb = ["from zz_prod import Hub", ""]
    for i in range(20):                       # and 20 test callers
        tb += [f"def test_case_{i}(h):", "    return h.target()", ""]
    (tmp_path / "aa_test.py").write_text("\n".join(tb))
    sp.run([sys.executable, "-m", "memway.cli", "init", str(tmp_path)],
           capture_output=True, cwd=str(HERE))

    from memway.query import before_edit
    b = before_edit(str(tmp_path), "Hub.target")

    shown, total = b["direct_callers_shown"], b["direct_callers_total"]
    assert total >= 30, f"fixture produced too few callers to bound: {total}"
    assert shown < total, "nothing was bounded, so nothing is being tested"
    assert shown == len(b["direct_callers"]), "the count disagrees with the list"
    assert b["direct_callers_tests"] > 0, "the fixture has no test callers"

    # RANKED: production first. A briefing that opens with test callers
    # buries the answer under the scaffolding.
    from memway.verify import is_test_entity
    from memway.query import _ctx
    _, _, ix, _, _ = _ctx(str(tmp_path))
    kinds = [is_test_entity(ix.entities[ix.by_qualname[c["qualname"]]])
             for c in b["direct_callers"]]
    assert not any(kinds[:5]), \
        f"tests appear in the first five callers: {[c['qualname'] for c in b['direct_callers'][:5]]}"

    # NOT DUPLICATED: the radius reports its shape, not the same names again.
    assert "direct" not in b["downstream"], \
        "downstream.direct is back - it repeated direct_callers verbatim"
    assert isinstance(b["downstream"].get("direct_count"), int)

    import json as _json
    assert len(_json.dumps(b)) < 12_000, \
        f"briefing is {len(_json.dumps(b)):,} chars - the cap is not holding"
