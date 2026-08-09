"""memway - a map of your codebase: coordinates, flow, and memory.

Quickstart:  pip install memway && memway setup .
             (builds the map, wires your agent, installs workflow rules)

Workflow: grep finds it; memway explains it and remembers it.

  memway setup [repo]                 one-command onboarding (see above)
  memway init <repo>                  build/refresh the map
  memway index <repo>                 re-index (incremental)
  memway harvest <repo>               mine docstrings + git history
  memway at <repo> <file:line>        grep hit -> entity (the handoff)
  memway show <repo> <ref>            entity dossier: edges + knowledge
  memway meta <repo> <ref> <ch> <txt> attach knowledge at a coordinate
                                        [--author WHO] (default: cli)
  memway pull <name>[@version]        fetch a published map into .coord
                                        [--into DIR] [--source URL]
                                        [--force] replace the derived index;
                                          local knowledge is merged, not lost
                                        [--replace-meta] DELETES locally
                                          authored knowledge
  memway lineage <repo> [ref]         identity history through renames
  memway mcp [repo]                   run the MCP server (agent wiring)
  memway --json <q> <repo> [args]     structured: summary, at, show,
                                        before-edit, lineage

Agent integration (Claude Code, Cursor - see IDE_AGENTS.md):
  claude mcp add memway -- memway mcp .
"""

import json
import sys
from pathlib import Path

from .indexer import Indexer
from .edges import EdgeBuilder, neighbors
from .metadata import MetaStore
from .lineage import VersionStore, detect_lineage


def _paths(repo):
    repo = Path(repo).resolve()
    return repo, repo / ".coord", repo / ".agents"


def _load(repo, must_exist=True):
    repo, coord, agents_dir = _paths(repo)
    if must_exist and not (coord / "index" / "coordinates.json").exists():
        raise SystemExit(f"no index at {repo} - run: memway init {repo}")
    ix = Indexer(repo, coord)
    try:
        ix.load_existing()
    except Exception as e:
        raise SystemExit(
            f"index unreadable ({type(e).__name__}) - run: "
            f"memway init {repo}  (identities recover from snapshots)")
    edges = EdgeBuilder.load(coord)
    meta = MetaStore(coord)
    return repo, coord, ix, edges, meta, None


def cmd_init(repo):
    repo, coord, _ = _paths(repo)
    (coord / "index").mkdir(parents=True, exist_ok=True)
    (coord / "manifest.json").write_text(json.dumps({
        "format": "memway/0.1", "language": "python",
    }, indent=2))
    print(f"initialized {coord}")
    cmd_index(repo)


def cmd_index(repo):
    repo, coord, ix, _, meta, _ = _load(repo, must_exist=False)
    report = ix.index()
    if report.get("parser_errors"):
        print("  WARNING: some language parsers are unavailable "
              "(files in these languages were skipped):")
        for lang, err in report["parser_errors"].items():
            print(f"    {lang}: {err}")
        print("    fix: pip install -e . (pins compatible grammar "
              "versions), or see IDE_AGENTS.md")
    if report.get("parse_errors"):
        errs = report["parse_errors"]
        print(f"  WARNING: {len(errs)} file(s) unparseable, skipped:")
        for path, err in errs[:5]:
            print(f"    {path}: {err[:70]}")
        if len(errs) > 5:
            print(f"    ... and {len(errs)-5} more")
    ix.save()
    eb = EdgeBuilder(ix)
    eb.build()
    eb.save(coord)

    store = VersionStore(coord)
    lineage = detect_lineage(report, ix, store, meta)
    v = store.snapshot()

    from .metrics import MetricsStore
    ms = MetricsStore(coord)
    mreport = ms.compute(ix, eb.edges, repo)
    # D7: stamp dirty-tree awareness
    import subprocess as _sp
    try:
        out = _sp.run(["git", "-C", str(repo), "status",
                       "--porcelain"], capture_output=True,
                      text=True, timeout=10).stdout
        dirty = any(l for l in out.splitlines()
                    if l[3:].split("/")[0] not in (".coord", ".agents"))
    except Exception:
        dirty = False
    ms.flag_dirty_tree(dirty)
    if not (repo / ".git").exists():
        print("  note: no git history here - churn stays 0 "
              "(clone with history to enable churn metrics)")
    if dirty:
        print("  note: working tree has uncommitted changes; "
              "churn/lineage describe committed state")

    print(f"indexed {len(ix.entities)} entities, {len(eb.edges)} edges -> v{v}")
    if hasattr(ix, "_cache_hits"):
        print(f"  parse cache: {ix._cache_hits} files cached, "
              f"{ix._cache_misses} parsed")
    print(f"  metrics: {mreport['recomputed']} recomputed, "
          f"{mreport['memoized']} memoized (hash unchanged)")
    if report["added"]:
        print(f"  added:   {len(report['added'])}")
    if report["removed"]:
        print(f"  removed: {len(report['removed'])}")
    if report["changed"]:
        print(f"  changed: {len(report['changed'])}")
    for l in lineage:
        print(f"  lineage: {l['kind']:8s} {l['note']}")


def cmd_show(repo, ref):
    repo, coord, ix, edges, meta, reg = _load(repo)
    e = ix.resolve(ref)
    if not e:
        print(f"no entity matches {ref!r}")
        return
    print(f"{e.coord_id}  {e.kind}  {e.qualname}")
    print(f"  at {e.path}:{e.lineno}"
          + (f"  sig={e.signature}" if e.signature else ""))
    for edge in neighbors(edges, e.coord_id):
        other = edge["dst"] if edge["src"] == e.coord_id else edge["src"]
        direction = "->" if edge["src"] == e.coord_id else "<-"
        label = other
        if not str(other).startswith("EVT:") and other in ix.entities:
            label = f"{other} ({ix.entities[other].qualname})"
        print(f"  {edge['kind']:9s} {direction} {label}")
    md = meta.read_all(e.coord_id, current_hash={getattr(e, "logic_hash", ""), e.body_hash})
    for channel, entries in md.items():
        print(f"  [{channel}]")
        for entry in entries:
            flag = " [STALE: code changed since written]" \
                if entry.get("stale") else ""
            print(f"    {entry['ts']} ({entry['author']}){flag} "
                  f"{entry['text']}")


def cmd_meta(repo, ref, channel, text, author="cli"):
    """Attach knowledge at a coordinate.

    author defaults to "cli", NOT "human": MetaStore.add's own default is
    "human", so every CLI write silently claimed human review. Five
    confirm entries in this repo were stamped that way by an agent
    driving the CLI. A confirm is an attestation - who vouched is the
    entire content of it - so the interface must not assert a person was
    involved when it cannot know. Pass --author to say who really did.
    """
    # import, do not duplicate: this list had drifted from metadata.CHANNELS
    # and omitted 'confirm', which is the ONLY way to clear a comment-rot
    # flag (see query.before_edit). Rot was therefore permanent for anyone
    # working through the CLI, while agents on MCP could clear it fine.
    from .metadata import CHANNELS
    if channel not in CHANNELS:
        raise SystemExit(f'unknown channel {channel!r} - '
                         f'one of: {", ".join(CHANNELS)}')
    repo, coord, ix, edges, meta, _ = _load(repo)
    e = ix.resolve(ref)
    if not e:
        print(f"no entity matches {ref!r}")
        return
    meta.add(e.coord_id, channel, text, author=author,
             body_hash=e.body_hash)
    print(f"added {channel} entry to {e.coord_id} ({e.qualname})")



def cmd_pull(name, into=".", source=None, force=False,
             replace_meta=False):
    """Fetch a published map and install it into <into>/.coord.

    A map is worth more when you do not have to build it: someone
    indexes a large dependency once and everyone else inherits the
    coordinates and the knowledge attached to them.
    """
    from .registry import pull, PullError, DEFAULT_SOURCE
    try:
        r = pull(name, into=into, source=source or DEFAULT_SOURCE,
                 force=bool(force) or bool(replace_meta),
                 replace_meta=bool(replace_meta))
    except PullError as e:
        raise SystemExit(f"pull failed: {e}")
    except Exception as e:
        raise SystemExit(f"pull failed: {type(e).__name__}: {e}")

    ents = r["entities"]
    print(f"installed {r['name']}@{r['version']} -> {r['installed_to']}")
    print(f"  {ents if ents is not None else 'unknown'} entities"
          f"  |  {r['members']} files  |  sha256 {r['sha256'][:16]}...")
    if r.get("repo"):
        print(f"  source repo: {r['repo']}"
              + (f" @ {r['sha'][:12]}" if r.get("sha") else ""))
    m = r.get("merged")
    if m is not None:
        print(f"  knowledge merged: +{m['entries_added']} entries, "
              f"{m['coords_from_bundle']} new coordinates; "
              f"{m['coords_local_kept']} local coordinate(s) preserved")
    elif r.get("replaced_meta"):
        print("  knowledge REPLACED: locally authored entries were deleted")
    if r.get("drifted"):
        # Honesty at the seam: the map describes a commit, the working
        # tree is at another. Staleness machinery handles the rest, but
        # silence here would let someone trust a map for code it never saw.
        print(f"  note: this map describes {str(r['sha'])[:12]}; your tree is "
              f"at {r['local_head'][:12]} - local code may have drifted")


def cmd_at(repo, location):
    """file:line -> the entity containing it (the grep handoff)."""
    from . import query
    d = query.at(repo, location)
    if "error" in d:
        sys.exit(d["error"])
    e = d["entity"]
    print(f"{d['location']} -> {e['qualname']}  [{e['coord_id']}]")
    print(f"  {e['kind']}  {e['path']}:{e['line']}-{e['line_end']}")
    if e.get("signature"):
        print(f"  sig={e['signature']}")
    for enc in d.get("enclosing", []):
        print(f"  within {enc}")
    for k in e.get("knowledge", [])[:4]:
        tag = " [STALE]" if k["stale"] else ""
        print(f"  {k['channel']}{tag}: {k['text'][:70]}")


def cmd_lineage(repo, ref=None):
    repo, coord, ix, edges, meta, _ = _load(repo)
    store = VersionStore(coord)
    if ref:
        e = ix.resolve(ref)
        cid = e.coord_id if e else ref
        chain = store.ancestry(cid)
        if not chain:
            print(f"no lineage recorded for {cid}")
        for entry in chain:
            print(f"v{entry['version']} {entry['kind']:8s} "
                  f"{entry['old']} -> {entry['new']}  {entry['note']}")
    else:
        for entry in store.read():
            print(f"v{entry['version']} {entry['kind']:8s} "
                  f"{entry['old']} -> {entry['new']}  {entry['note']}")



def cmd_harvest(repo):
    from .harvest import Harvester
    from .lineage import VersionStore
    repo, coord, ix, edges, meta, _ = _load(repo)
    ix.load_raw_edges()   # D8: persisted at index time, no re-index
    h = Harvester(ix, meta, VersionStore(coord), repo)
    stats = h.run()
    print("harvested: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    print("(provenance-tagged; re-running skips already-mined entries)")


# The three measured rules (Phase B of the write-back experiment):
# unconfigured agents execute perfectly and remember nothing; these
# lines convert that into before-edit briefings, verified changes,
# and reasons that outlive the session. Exact tool names matter -
# agents should not have to guess (finding #14).
_AGENT_RULES = """\
# Project rules

This repo uses memway (MCP tools prefixed `memway_`) as its
memory layer.

- Before editing any function or class, call `memway_before_edit`
  on it and heed any attached knowledge. If `memway_before_edit`
  returns an error, do NOT edit - resolve the ref first (try a
  bare function name, module.qualname, or memway_at <file:line>)
  and retry before_edit.
- After completing changes, call `memway_verify_change` to confirm
  impact.
- When a task, design doc, or conversation supplies a REASON a piece
  of code is the way it is (constraints, incidents, tuning
  rationale), record that reason with `memway_meta` on the relevant
  entity - reasons must outlive this session. This is due whenever a
  reason or finding SURFACES, not only when a change lands: tasks you
  decline, block on, investigate, or leave unfinished count too. The
  reason a change was refused is often the most valuable thing to
  record - a constraint strong enough to stop work is exactly what the
  next session needs and exactly what the code cannot say on its own.
  Capture it before you reply.
"""

# Portable wiring: relies on the `memway` console script being on
# PATH and on agents launching MCP servers with cwd = repo root, so
# the file survives clones and moves (no absolute paths, no venv
# paths). This is what lets a committed map travel with the repo.
_MCP_JSON = {
    "mcpServers": {
        "memway": {"command": "memway", "args": ["mcp", "."]}
    }
}


def cmd_setup(repo="."):
    """One-command onboarding: map + agent wiring + workflow rules.
    Idempotent - never overwrites files the user already has."""
    repo_p = Path(repo).resolve()
    if (repo_p / ".coord").exists():
        print(f"map exists at {repo_p / '.coord'} - leaving it")
    else:
        cmd_init(repo)
    mcp_file = repo_p / ".mcp.json"
    if mcp_file.exists():
        print(".mcp.json exists - leaving it")
    else:
        mcp_file.write_text(json.dumps(_MCP_JSON, indent=2) + "\n")
        print("wrote .mcp.json (agent server wiring)")
    rules = repo_p / "CLAUDE.md"
    if rules.exists():
        print("CLAUDE.md exists - leaving it "
              "(memway rules: see `memway` usage text)")
    else:
        rules.write_text(_AGENT_RULES)
        print("wrote CLAUDE.md (the three measured workflow rules)")
    print("\nnext steps:")
    print("  1. restart your agent in this directory "
          "(it will pick up .mcp.json)")
    print('  2. ask it: "what does this repo know?"')


def cmd_mcp(repo="."):
    """Run the MCP server (what .mcp.json launches)."""
    from . import mcp as _mcp_mod
    if not (Path(repo) / ".coord").exists():
        sys.stderr.write(f"memway: no index at {repo}; "
                         f"run 'memway init {repo}' first\n")
        sys.exit(1)
    _mcp_mod.serve(repo)


COMMANDS = {
    "init": cmd_init, "index": cmd_index, "harvest": cmd_harvest,
    "show": cmd_show, "meta": cmd_meta, "lineage": cmd_lineage,
    "at": cmd_at, "setup": cmd_setup, "mcp": cmd_mcp,
    "pull": cmd_pull,
}


def main():
    import signal
    if hasattr(signal, "SIGPIPE"):          # D6
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = sys.argv[1:]
    if "--json" in args:
        import json as _json
        from . import query
        args = [a for a in args if a != "--json"]
        cmd = args[0] if args else ""
        if cmd not in query.QUERIES:
            print(_json.dumps({"error": f"no JSON query for {cmd!r}; "
                  f"available: {sorted(query.QUERIES)}"}))
            sys.exit(1)
        repo = args[1] if len(args) > 1 else "."
        rest = args[2:]
        try:
            print(_json.dumps(query.QUERIES[cmd](repo, rest), indent=2))
        except Exception as e:
            print(_json.dumps({"error": f"{type(e).__name__}: {e}"}))
            sys.exit(1)
        return
    # Flags are pulled out before dispatch because COMMANDS entries are
    # called with positional argv passthrough. Each flag declares which
    # command owns it; anywhere else it is a typo worth failing on rather
    # than silently ignoring.
    VALUE_FLAGS = {"--author": "meta", "--source": "pull", "--into": "pull"}
    BOOL_FLAGS = {"--force": "pull", "--replace-meta": "pull"}
    opts, owners = {}, {}
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(args):
            name = a.split("=", 1)[0]
            if name in VALUE_FLAGS:
                if "=" in a:
                    opts[name[2:]] = a.split("=", 1)[1]
                    args = args[:i] + args[i + 1:]
                elif i + 1 < len(args):
                    opts[name[2:]] = args[i + 1]
                    args = args[:i] + args[i + 2:]
                else:
                    sys.stderr.write(f"{name} needs a value\n")
                    sys.exit(1)
                owners[name] = VALUE_FLAGS[name]
                changed = True
                break
            if name in BOOL_FLAGS:
                opts[name[2:].replace("-", "_")] = True
                owners[name] = BOOL_FLAGS[name]
                args = args[:i] + args[i + 1:]
                changed = True
                break
    if not args or args[0] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    wrong = [f for f, owner in owners.items() if owner != args[0]]
    if wrong:
        sys.stderr.write(f"{', '.join(sorted(wrong))} "
                         f"applies to '{owners[wrong[0]]}' only\n")
        sys.exit(1)
    COMMANDS[args[0]](*args[1:], **opts)


if __name__ == "__main__":
    main()
