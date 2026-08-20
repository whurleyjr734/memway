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


def test_one_truncation_rule_and_nothing_reimplements_it():
    """rank-bound-report has ONE implementation.

    It was written by hand five times before payload.py existed:
    attention capped markers and comment rot with two differently named
    totals, summary sliced knowledge entries at 20 and two hot lists at 5
    and reported NOTHING, and before_edit had just grown a sixth. Five
    copies is how a rule gets fixed in one place and not the others -
    the shape this codebase already pins against for the stamping rules
    (stamp_for / accepted_for) and the ring rule.

    Pinned structurally: the reporting keys are built inside
    payload.rank_bound_report from the list's own name, so no other module
    may construct a `<name>_shown` key. A hand-rolled cap elsewhere would
    have to invent one, and this test would name it.
    """
    import ast
    from memway import payload
    src_dir = HERE / "memway"

    assert callable(payload.rank_bound_report)
    shown, rep = payload.rank_bound_report([3, 1, 2], "things", rank=lambda x: x)
    assert shown == [1, 2, 3], "rank is not applied"
    assert rep == {"things_total": 3, "things_shown": 3}, rep
    shown, rep = payload.rank_bound_report(list(range(50)), "things")
    assert len(shown) == payload.CAP and rep["things_total"] == 50, rep

    offenders = []
    for f in sorted(src_dir.glob("*.py")):
        if f.name == "payload.py":
            continue
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            # a literal "<something>_shown" key anywhere else means a
            # second implementation of the report half
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value.endswith("_shown"):
                offenders.append(f"{f.name}: {node.value!r}")
    assert not offenders, (
        "these build a truncation report outside payload.py, which is how "
        f"five copies of this rule happened the first time: {offenders}")


def test_every_payload_surface_keeps_its_shape():
    """The fields agents read, asserted - because none of them were.

    A 96% payload reduction landed with the suite green: `direct_callers`
    was referenced by exactly ONE assertion in the whole suite and
    `downstream.direct` by none, so removing a field and truncating a list
    to twelve went unnoticed. This is the guard that objects next time.

    Deliberately shape, not content: names and types of the keys a caller
    binds to, so the test survives real changes to the numbers.
    """
    import subprocess as sp
    from memway import query
    repo = HERE
    contracts = {
        # NOTE: no "repo" key - summary has never had one. The first
        # draft of this contract asserted it and failed, which is the
        # test working: a shape test is only worth having if it is
        # written against the payload rather than against memory of it.
        "summary": (query.summary(str(repo)), [
            "entities", "edges", "kinds", "languages",
            "hardest", "hardest_total", "hardest_shown",
            "hardest_overall", "hardest_overall_total",
            "entities_by_origin", "knowledge", "map_lag"]),
        "attention": (query.attention(str(repo)), [
            "comment_rot", "comment_rot_total", "comment_rot_shown",
            "markers", "markers_total", "markers_shown", "marker_total",
            "stale_design_docs", "stale_notes", "note"]),
        "show": (query.show(str(repo), "memway.query.before_edit"), [
            "coord_id", "qualname", "kind", "path", "line", "signature",
            "edges", "edges_total", "edges_shown", "knowledge",
            "map_lag", "knowledge_lag"]),
        "before_edit": (query.before_edit(str(repo), "memway.query.before_edit"), [
            "entity", "metrics", "comments", "grounding", "knowledge",
            "direct_callers", "direct_callers_total", "direct_callers_shown",
            "direct_callers_tests", "downstream", "warnings"]),
    }
    for tool, (payload, keys) in contracts.items():
        missing = [k for k in keys if k not in payload]
        assert not missing, f"{tool} lost payload keys: {missing}"

    s = contracts["summary"][0]
    assert {"entries", "entries_total", "entries_shown"} <= set(s["knowledge"])
    d = contracts["before_edit"][0]["downstream"]
    assert "direct_count" in d and "direct" not in d, \
        "downstream.direct duplicated direct_callers verbatim; it must not return"
    from memway.payload import CAP
    bounded_somewhere = False
    for tool, (payload, _) in contracts.items():
        for k, v in payload.items():
            if not k.endswith("_shown"):
                continue
            base = k[:-len("_shown")]
            total = payload.get(f"{base}_total", 0)
            assert total >= v, f"{tool}.{k} exceeds its own total"
            # THE BOUND ITSELF, not just the arithmetic. Asserting only
            # shown <= total is satisfied by shipping everything and
            # reporting it - cap=None passed that check, which is how
            # this assertion earned its place.
            assert v <= max(CAP, 5), \
                f"{tool}.{base} shipped {v} entries - the cap is not holding"
            if total > v:
                bounded_somewhere = True
    assert bounded_somewhere, (
        "no list in any payload was actually truncated, so this test "
        "cannot tell a bounded surface from an unbounded one")


def test_the_caller_warning_carries_its_own_confidence(tmp_path):
    """"573 direct callers" and "573 name guesses" are different claims.

    Measured on django@cccc004:
    django.contrib.gis.geos.mutable_list.ListMixin.append has 573 direct
    callers and ALL 573 are bare-name guesses at confidence 0.6 -
    ordinary `results.append(x)` across the codebase landing on a GIS
    mixin. The RESOLVER is right there: it could not type the receiver
    and refused to claim certainty, which is why they are 0.6 and not
    0.95.

    What was wrong is the sentence built from the number. The briefing
    led with "WIDELY DEPENDED ON (573 direct callers)" while the fact
    that every one was a guess sat in a separate grounding block, phrased
    about the whole radius rather than about that number. The reader gets
    the claim and the caveat in different places and weighs the claim.

    The threshold is LOW_CONFIDENCE, shared with the grounding block - a
    second literal is how two surfaces come to disagree about what a
    guess is.
    """
    import subprocess as sp
    # REACHING THE BARE-NAME TIER TAKES A SPECIFIC SHAPE, and getting it
    # wrong makes this test pass on nothing. Two same-named METHODS is not
    # enough: resolve() breaks that tie itself (production over test) and
    # reports "exact" at 0.95, so the fallback never runs and the fixture
    # produced zero guessed callers.
    #
    # What is needed is for resolve() to go ambiguous AND for the
    # fallback's filter to leave exactly one candidate. A method plus a
    # module-level FUNCTION of the same name does both: resolve() sees two
    # and refuses, then rule 2 ("an attribute call is not a module-level
    # function") removes the function, leaving the method at 0.60 -
    # exactly django's ListMixin.append.
    body = ["class Alpha:", "    def stash(self, x):", "        return x", "",
            "def stash(x):", "    return x", ""]
    for i in range(8):
        body += [f"def caller_{i}(o):", "    return o.stash(1)", ""]
    (tmp_path / "m.py").write_text("\n".join(body))
    sp.run([sys.executable, "-m", "memway.cli", "init", str(tmp_path)],
           capture_output=True, cwd=str(HERE))

    from memway.query import before_edit, LOW_CONFIDENCE
    b = before_edit(str(tmp_path), "m.Alpha.stash")
    total = b["direct_callers_total"]
    guessed = b["direct_callers_guessed"]
    assert total >= 5, (
        f"fixture produced {total} callers; it must produce at least 5 or "
        f"the warning never fires and this test asserts nothing")
    assert guessed == total, (
        f"fixture produced {guessed} guessed of {total} - it is not "
        f"reaching the bare-name tier, so the branch under test never runs")
    wide = [w for w in b["warnings"] if w.startswith("WIDELY DEPENDED ON")]
    assert wide, "no widely-depended-on warning to qualify"
    assert "guesses" in wide[0] and "upper bound" in wide[0], (
        f"{total} callers are ALL guesses and the warning does not say so: "
        f"{wide[0]!r}")
    assert 0 < LOW_CONFIDENCE <= 1


def test_inheritance_lists_are_bounded_too(tmp_path):
    """The 0.58.0 census missed this field, and the reason is instructive.

    It measured `before_edit` on a Go entity with no inheritance at all,
    so `inherited_unchanged_by` never appeared. On django@cccc004,
    SimpleTestCase.assertRaisesMessage is inherited unchanged by 2,389
    test classes and that ONE field was 133,163 of the briefing's 136,453
    characters - 97.6%. A census is only as good as the entity it runs on.
    """
    import subprocess as sp
    body = ["class Base:", "    def run(self):", "        return 1", ""]
    for i in range(30):
        body += [f"class Sub{i}(Base):", "    pass", ""]
    (tmp_path / "m.py").write_text("\n".join(body))
    sp.run([sys.executable, "-m", "memway.cli", "init", str(tmp_path)],
           capture_output=True, cwd=str(HERE))

    from memway.query import before_edit
    from memway.payload import CAP
    b = before_edit(str(tmp_path), "Base.run")
    inh = b.get("inheritance") or {}
    total = inh.get("inherited_unchanged_by_total", 0)
    if total <= CAP:
        pytest.skip(f"fixture produced {total} inheritors; need more than {CAP}")
    assert len(inh["inherited_unchanged_by"]) == CAP, \
        f"the list is not bounded: {len(inh['inherited_unchanged_by'])} of {total}"
    assert inh["inherited_unchanged_by_shown"] == CAP
    assert "overridden_by_total" in inh, "the sibling list lost its report"
