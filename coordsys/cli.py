"""coordsys - a map of your codebase: coordinates, flow, and memory.

Workflow: grep finds it; coordsys explains it and remembers it.

  coordsys init <repo>                  build/refresh the map
  coordsys index <repo>                 re-index (incremental)
  coordsys harvest <repo>               mine docstrings + git history
  coordsys at <repo> <file:line>        grep hit -> entity (the handoff)
  coordsys show <repo> <ref>            entity dossier: edges + knowledge
  coordsys meta <repo> <ref> <ch> <txt> attach knowledge at a coordinate
  coordsys lineage <repo> [ref]         identity history through renames
  coordsys --json <q> <repo> [args]     structured: summary, at, show,
                                        before-edit, lineage

Agent integration (Claude Code, Cursor - see IDE_AGENTS.md):
  claude mcp add coordsys -- python3 -m coordsys.mcp <repo>
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
        raise SystemExit(f"no index at {repo} - run: coordsys init {repo}")
    ix = Indexer(repo, coord)
    try:
        ix.load_existing()
    except Exception as e:
        raise SystemExit(
            f"index unreadable ({type(e).__name__}) - run: "
            f"coordsys init {repo}  (identities recover from snapshots)")
    edges = EdgeBuilder.load(coord)
    meta = MetaStore(coord)
    return repo, coord, ix, edges, meta, None


def cmd_init(repo):
    repo, coord, _ = _paths(repo)
    (coord / "index").mkdir(parents=True, exist_ok=True)
    (coord / "manifest.json").write_text(json.dumps({
        "format": "coordsys/0.1", "language": "python",
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


def cmd_meta(repo, ref, channel, text):
    CHANNELS = ("docs", "notes", "history", "design", "traces")
    if channel not in CHANNELS:
        raise SystemExit(f'unknown channel {channel!r} - '
                         f'one of: {", ".join(CHANNELS)}')
    repo, coord, ix, edges, meta, _ = _load(repo)
    e = ix.resolve(ref)
    if not e:
        print(f"no entity matches {ref!r}")
        return
    meta.add(e.coord_id, channel, text, body_hash=e.body_hash)
    print(f"added {channel} entry to {e.coord_id} ({e.qualname})")



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


COMMANDS = {
    "init": cmd_init, "index": cmd_index, "harvest": cmd_harvest,
    "show": cmd_show, "meta": cmd_meta, "lineage": cmd_lineage,
    "at": cmd_at,
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
    if len(args) < 2 or args[0] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[args[0]](*args[1:])


if __name__ == "__main__":
    main()
