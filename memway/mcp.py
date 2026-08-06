"""memway MCP server: the map as tools an IDE agent calls autonomously.

Speaks the Model Context Protocol over stdio, so Claude Code, Cursor,
Windsurf, and Zed can all use memway without bespoke integration.
Every tool is a thin wrapper over memway.query (the same structured
core the CLI's --json path uses), so agent-facing and human-facing
answers can never drift.

Design: read-only tools only. The agent inspects the map; it never
mutates the index through this surface. The single most valuable tool
is `memway_summary` - an agent should call it FIRST on any repo to
get the ~structured briefing instead of grepping blind.

Run:  python -m memway.mcp /path/to/repo
Register in Claude Code:  claude mcp add memway -- python -m memway.mcp <repo>
(The repo path is bound at launch so the agent's tool calls need only
pass refs, not the repo root, every time.)
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import query


TOOLS = [
    {
        "name": "memway_summary",
        "description": "Repo shape at a glance: entity/edge counts, "
                       "languages, and the hardest (most complex) "
                       "functions. CALL THIS FIRST on an unfamiliar "
                       "repo instead of reading files blind.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": lambda repo, a: query.summary(repo),
    },
    {
        "name": "memway_at",
        "description": "The grep handoff: given file:line (e.g. "
                       "'src/app.py:42' from a grep hit or stack "
                       "trace), return the entity containing that "
                       "line - its coordinate, signature, edges, and "
                       "attached knowledge. Use grep to FIND code, "
                       "then this to enter the map at the hit.",
        "inputSchema": {"type": "object", "properties": {
            "location": {"type": "string"}}, "required": ["location"]},
        "fn": lambda repo, a: query.at(repo, a["location"]),
    },
    {
        "name": "memway_before_edit",
        "description": "CALL THIS BEFORE MODIFYING ANY ENTITY. One "
                       "pre-change safety briefing: the entity's "
                       "location and signature, its complexity, every "
                       "direct caller, the blast radius of a change "
                       "(with lower-bound honesty), team knowledge "
                       "attached to it (staleness flagged), recent "
                       "renames/moves, and explicit WARNINGS when the "
                       "edit is riskier than it looks.",
        "inputSchema": {"type": "object", "properties": {
            "ref": {"type": "string"}}, "required": ["ref"]},
        "fn": lambda repo, a: query.before_edit(repo, a["ref"]),
    },
    {
        "name": "memway_verify_change",
        "description": "CALL THIS AFTER MODIFYING CODE. Diffs the working "
                       "tree against the saved index, traces the change's "
                       "reverse reachability over the edge graph, and "
                       "returns the tests covering that flow - tiered: "
                       "'grounded' (reached via real edges) vs 'name_hit' "
                       "(mentioned by name only; a labeled guess). Set "
                       "run=true to execute exactly that selection and get "
                       "a pass/fail summary. Loop: before_edit -> edit -> "
                       "verify_change.",
        "inputSchema": {"type": "object", "properties": {
            "run": {"type": "boolean"}}},
        "fn": lambda repo, a: query.verify_change(repo,
                                                  bool(a.get("run"))),
    },
    {
        "name": "memway_probe",
        "description": "Exec on steroids: RUN an entity with real values "
                       "and get the end-to-end data flow back as "
                       "coordinates. Pass args/kwargs as JSON, or "
                       "'$expr:<python>' evaluated against `setup` code "
                       "(build instances there to probe methods). Returns "
                       "the return value (or the coordinate that raised), "
                       "the ordered call tree with per-entity args and "
                       "returns, and flow_vs_map: runtime-observed edges "
                       "diffed against the static graph. discovered_edges "
                       "are dynamic calls static analysis missed; set "
                       "record=true to write them back at confidence 1.0. "
                       "This EXECUTES repo code - same trust boundary as "
                       "running its tests.",
        "inputSchema": {"type": "object", "properties": {
            "ref": {"type": "string"},
            "args": {"type": "array"},
            "kwargs": {"type": "object"},
            "setup": {"type": "string"},
            "record": {"type": "boolean"}},
            "required": ["ref"]},
        "fn": lambda repo, a: query.probe(repo, a["ref"],
                                          a.get("args"), a.get("kwargs"),
                                          a.get("setup", ""),
                                          bool(a.get("record"))),
    },
    {
        "name": "memway_meta",
        "description": "WRITE BACK an observation for future readers "
                       "(human or agent). Attach a note to an entity's "
                       "coordinate - e.g. a subtle bug you noticed, why "
                       "a threshold has its value, a gotcha the code "
                       "doesn't state. Notes are body-hash-stamped: if "
                       "the code later changes they are flagged stale, "
                       "never silently wrong. Channels: 'notes' (default) "
                       "for observations, 'docs' for explanations. Your "
                       "notes appear in future before_edit briefings at "
                       "this coordinate - this is how the map accumulates "
                       "the WHY that code alone cannot carry.",
        "inputSchema": {"type": "object", "properties": {
            "ref": {"type": "string"},
            "channel": {"type": "string"},
            "text": {"type": "string"}},
            "required": ["ref", "text"]},
        "fn": lambda repo, a: query.agent_meta(repo, a["ref"],
                                               a.get("channel", "notes"),
                                               a["text"]),
    },
    {
        "name": "memway_attention",
        "description": "The repo's ATTENTION QUEUE in one call: entities "
                       "whose comments went stale across behavior changes "
                       "(comment rot), all TODO/FIXME/HACK markers with "
                       "their coordinates, design docs whose governed "
                       "entities drifted, and stale knowledge notes. Every "
                       "item is a place where recorded intent and current "
                       "behavior may disagree. Call it at session start or "
                       "when asked 'what needs attention in this repo'.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer"}}},
        "fn": lambda repo, a: query.attention(repo,
                                              int(a.get("limit", 20))),
    },
    {
        "name": "memway_show",
        "description": "Full dossier for one entity (by qualname or "
                       "coordinate id): its signature, callers/callees, "
                       "and any attached knowledge (docs, notes, "
                       "history), with staleness flagged.",
        "inputSchema": {"type": "object", "properties": {
            "ref": {"type": "string"}}, "required": ["ref"]},
        "fn": lambda repo, a: query.show(repo, a["ref"]),
    },
    {
        "name": "memway_lineage",
        "description": "The identity history of an entity: renames, "
                       "moves, and prior versions. Answers 'what was "
                       "this called before?' across refactors.",
        "inputSchema": {"type": "object", "properties": {
            "ref": {"type": "string"}}, "required": ["ref"]},
        "fn": lambda repo, a: query.lineage(repo, a["ref"]),
    },
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def _result(id_, payload):
    return {"jsonrpc": "2.0", "id": id_, "result": payload}


def _error(id_, code, msg):
    return {"jsonrpc": "2.0", "id": id_,
            "error": {"code": code, "message": msg}}


def handle(msg: dict, repo: str) -> dict | None:
    """Handle one JSON-RPC message. Returns a response dict, or None
    for notifications (no id)."""
    method = msg.get("method")
    id_ = msg.get("id")

    if method == "initialize":
        return _result(id_, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "memway", "version": "1.0"},
        })
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _result(id_, {"tools": [
            {k: t[k] for k in ("name", "description", "inputSchema")}
            for t in TOOLS]})
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        tool = _BY_NAME.get(name)
        if not tool:
            return _error(id_, -32601, f"unknown tool {name!r}")
        import json as _json
        from .flightlog import record
        args = params.get("arguments", {})
        try:
            data = tool["fn"](repo, args)
            record(repo, name, args,
                   ok=not (isinstance(data, dict) and "error" in data))
            return _result(id_, {"content": [
                {"type": "text", "text": _json.dumps(data, indent=2)}]})
        except Exception as e:                       # never crash the agent
            record(repo, name, args, ok=False)
            return _result(id_, {"content": [
                {"type": "text",
                 "text": _json.dumps({"error": f"{type(e).__name__}: {e}"})}],
                "isError": True})
    if id_ is not None:
        return _error(id_, -32601, f"unknown method {method!r}")
    return None


def serve(repo: str, stdin=None, stdout=None):
    """stdio JSON-RPC loop. One JSON object per line."""
    import json as _json
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        resp = handle(msg, repo)
        if resp is not None:
            stdout.write(_json.dumps(resp) + "\n")
            stdout.flush()


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    if not (Path(repo) / ".coord").exists():
        sys.stderr.write(
            f"memway: no index at {repo}; run 'memway init {repo}' "
            "first\n")
        sys.exit(1)
    serve(repo)


if __name__ == "__main__":
    main()
