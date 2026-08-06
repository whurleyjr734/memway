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

from coordsys import query, mcp, cli



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


def test_query_on_missing_index_returns_error(tmp_path):
    assert "error" in query.summary(str(tmp_path / "nope"))


# ----------------------------------------------------------- MCP protocol

def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method,
            "params": params or {}}


def test_initialize_handshake(built):
    r = mcp.handle(rpc("initialize"), built)
    assert r["result"]["serverInfo"]["name"] == "coordsys"
    assert "protocolVersion" in r["result"]


def test_initialized_notification_returns_none(built):
    assert mcp.handle(rpc("notifications/initialized", id_=None),
                      built) is None


def test_tools_list_advertises_every_tool(built):
    r = mcp.handle(rpc("tools/list"), built)
    names = {t["name"] for t in r["result"]["tools"]}
    assert names == {"coordsys_summary", "coordsys_at",
                     "coordsys_verify_change", "coordsys_probe", "coordsys_meta",
                     "coordsys_attention",
                     "coordsys_show", "coordsys_lineage",
                     "coordsys_before_edit"}
    for t in r["result"]["tools"]:                  # schema well-formed
        assert t["inputSchema"]["type"] == "object"


def test_tools_call_summary(built):
    r = mcp.handle(rpc("tools/call", {"name": "coordsys_summary",
                                      "arguments": {}}), built)
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["entities"] > 0


def test_tools_call_unknown_tool_errors(built):
    r = mcp.handle(rpc("tools/call", {"name": "nope", "arguments": {}}),
                   built)
    assert "error" in r


def test_tools_call_bad_args_returns_iserror_not_crash(built):
    r = mcp.handle(rpc("tools/call", {"name": "coordsys_show",
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
    assert lines[0]["result"]["serverInfo"]["name"] == "coordsys"
    assert len(lines[1]["result"]["tools"]) == 9


# ------------------------------------------------------- before_edit

def test_before_edit_briefing_shape_and_warnings(built):
    from coordsys import cli as _cli
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
    r = mcp.handle(rpc("tools/call", {"name": "coordsys_before_edit",
                                      "arguments": {"ref": "sign"}}),
                   built)
    payload = json.loads(r["result"]["content"][0]["text"])
    assert "warnings" in payload and "downstream" in payload


def test_summary_knowledge_census(built):
    """The census: summary answers "what does the map remember" -
    counts by channel, per-coordinate entries, freshness judged like
    before_edit. Uses the confirm channel when the tree has it."""
    from coordsys.metadata import CHANNELS
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

    call("coordsys_summary", {})
    call("coordsys_show", {"ref": "sign"})
    call("coordsys_show", {"ref": "no_such_entity_xyz"})

    log = Path(built) / ".coord" / "log" / "usage.jsonl"
    assert log.exists()
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    assert [l["tool"] for l in lines] == [
        "coordsys_summary", "coordsys_show", "coordsys_show"]
    assert lines[1]["ref"] == "sign"
    assert [l["ok"] for l in lines] == [True, True, False]
    assert len({l["session"] for l in lines}) == 1
    # reference-only contract: no payload text fields ever
    assert all(set(l) <= {"ts", "session", "tool", "ref", "ok"}
               for l in lines)
