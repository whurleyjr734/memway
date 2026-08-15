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
  memway console <repo> [--port N]    serve the map live: tools as buttons,
                                        notes written back from the card
                                        (127.0.0.1 + session token only)
  memway viz <repo> [--out F]         render the real map as a single
                                        interactive HTML file (read-only;
                                        --filter <qualname-prefix> for a
                                        subtree, --force above 1500
                                        entities; never samples silently)
  memway evidence <repo> <ref>        read cached evidence bodies
                                        (--clear removes ALL of it; the
                                         authored map is untouched)
  memway dig <repo> <ref> [--cache]   mine ONE entity's history: commits
                                        touching its exact range, forge PR
                                        bodies, release tags. Returns
                                        CANDIDATES - judging rationale vs
                                        restatement is the caller's job.
                                        Never gates, scores, or writes.
  memway mcp [repo]                   run the MCP server (agent wiring)
  memway --version                    print the installed version (-V)
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
        # QUOTED on purpose: unquoted, zsh globs the brackets and the
        # command a user copies fails with "no matches found".
        print("    fix: pip install 'memway[languages]'  "
              "(installs the tree-sitter grammars)")
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
    from .metadata import accepted_for
    md = meta.read_all(e.coord_id, current_hash=accepted_for(e))
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
    from .metadata import CHANNELS, stamp_for
    if channel not in CHANNELS:
        raise SystemExit(f'unknown channel {channel!r} - '
                         f'one of: {", ".join(CHANNELS)}')
    repo, coord, ix, edges, meta, _ = _load(repo)
    e = ix.resolve(ref)
    if not e:
        print(f"no entity matches {ref!r}")
        return
    meta.add(e.coord_id, channel, text, author=author,
             body_hash=stamp_for(e))
    print(f"added {channel} entry to {e.coord_id} ({e.qualname})")



def cmd_pull(name, into=".", source=None, force=False,
             replace_meta=False):
    """Fetch a published map and install it into <into>/.coord.

    A map is worth more when you do not have to build it: someone
    indexes a large dependency once and everyone else inherits the
    coordinates and the knowledge attached to them.

    MANIFEST v1 - every bundle carries .coord/manifest.json:

        name            the map's own name, e.g. "httpx"
        upstream_repo   URL of the repository that was indexed
        upstream_sha    the exact commit indexed; drift is measured
                        against it, not against the release tag
        memway_version  the memway that built the map
        license         upstream's license, carried with the map
        built_at        UTC ISO-8601 build time

    Bundles published before v1 carry `repo`/`sha` aliases instead;
    registry._describe reads those into the v1 names, so both shapes
    install and nothing downstream knows two schemas exist.

    There is deliberately no MCP tool for this. `pull` fetches over the
    network and writes a directory tree to disk; that pair stays behind
    a human typing a command, not behind a model deciding to call it.
    """
    from .registry import pull, PullError, DEFAULT_SOURCE
    try:
        # --replace-meta deliberately does NOT imply --force. The
        # destructive path should be harder to type than the safe one,
        # and typing both is the moment you notice which you asked for.
        if replace_meta and not force:
            raise SystemExit(
                "destructive: deletes locally authored knowledge; "
                "requires explicit --force")
        r = pull(name, into=into, source=source or DEFAULT_SOURCE,
                 force=bool(force), replace_meta=bool(replace_meta))
    except PullError as e:
        raise SystemExit(f"pull failed: {e}")
    except Exception as e:
        raise SystemExit(f"pull failed: {type(e).__name__}: {e}")

    ents = r["entities"]
    print(f"installed {r['name']}@{r['version']} -> {r['installed_to']}")
    print(f"  {ents if ents is not None else 'unknown'} entities"
          f"  |  {r['members']} files  |  sha256 {r['sha256'][:16]}...")
    if r.get("upstream_repo"):
        print(f"  source repo: {r['upstream_repo']}"
              + (f" @ {r['upstream_sha'][:12]}" if r.get("upstream_sha") else ""))
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
        print(f"  note: this map describes {str(r['upstream_sha'])[:12]}; your "
              f"tree is at {r['local_head'][:12]} - local code may have drifted")


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


def cmd_dig(repo, ref, *flags):
    """Demand-paged history for one entity. Candidates only - see dig.py."""
    from .dig import dig, REGION_HISTORY
    cache = "--cache" in flags
    for f in flags:
        if f != "--cache":
            raise SystemExit(f"unknown flag {f!r} - use --cache")
    out = dig(repo, ref, cache=cache)
    if "error" in out:
        print(out["error"])
        for c in out.get("closest", []):
            print(f"  did you mean: {c}")
        sys.exit(1)
    e, d, n = out["entity"], out["dig"], out["counts"]
    print(f"{e['coord_id']}  {e['qualname']}")
    print(f"  {e['path']}:{e['lineno']}-{e['end_lineno']}")
    print(f"  {d['command']}")
    print(f"  {n['total']} commits touched this range "
          f"({n['entity_history']} entity-history, "
          f"{n['region_history']} region-history)")
    for w in out.get("warnings", []):
        print(f"  note: {w}")
    if d["creation_boundary"]:
        print(f"  creation boundary: {d['creation_boundary'][:10]}")
    for note in out.get("notes", []):
        print(f"  ! {note}")
    print()
    for c in out["candidates"]:
        mark = "~" if c["provenance"] == REGION_HISTORY else " "
        print(f"{mark} {c['short_sha']}  {c['date']}  {c['subject'][:66]}")
        if c["provenance"] == REGION_HISTORY:
            print(f"    [{REGION_HISTORY}]")
        for r in c.get("pr_refs", []):
            if r.get("body"):
                print(f"    PR #{r['number']}: {r['body'].splitlines()[0][:60] if r['body'].strip() else '(empty)'}")
            else:
                print(f"    PR #{r['number']}: unavailable "
                      f"({r.get('unavailable_reason')})")
        for w in c.get("warnings", []):
            print(f"    ! {w}")
    if out.get("evidence"):
        ev = out["evidence"]
        if ev.get("cache_hit"):
            print(f"\n  served from the evidence cache "
                  f"({ev['stored']} records) - no history walked")
        else:
            print(f"\n  cached {ev['stored']} evidence records "
                  f"(+{ev['added']} new) through "
                  f"{(ev.get('dug_through_sha') or '')[:10]}")
    print("\ncandidates only - judging rationale vs restatement, and writing "
          "anything back to the map, is YOUR job. This tool never gates, "
          "scores, or writes.")


def cmd_evidence(repo, ref="", which=""):
    """Read cached evidence bodies. Bodies live once, here."""
    from . import evidence as ev
    from .indexer import Indexer
    from pathlib import Path as _P
    repo_p = _P(repo).resolve()
    coord = repo_p / ".coord"
    if "--clear" in (ref, which):
        r = ev.clear(coord)
        print(f"cleared {r['cleared']} evidence records across "
              f"{r['coordinates']} coordinate(s).")
        print("  authored knowledge in .coord/meta is untouched - evidence "
              "is a sibling directory, not a child.")
        return
    if not ref:
        raise SystemExit("usage: memway evidence <repo> <ref> | --clear")
    ix = Indexer(repo_p, coord)
    ix.load_existing()
    e = ix.resolve(ref)
    if e is None:
        # a bare sha/PR ref: search every coordinate's evidence for it
        for f in sorted(ev.evidence_root(coord).glob("*.jsonl")):
            recs = ev.read(coord, f.stem)
            hit = ev.index_by_ref(recs).get(ref) or \
                ev.index_by_ref(recs).get(ref.lstrip("#"))
            if hit:
                print(f"{hit.get('source')} "
                      f"{hit.get('short_sha') or '#'+str(hit.get('number'))}"
                      f"  {hit.get('date')}  {hit.get('author')}")
                print(f"  {hit.get('subject')}\n")
                print(hit.get("body") or "(no body)")
                return
        raise SystemExit(f"no entity or cached evidence matches {ref!r}")
    recs = ev.read(coord, e.coord_id)
    if not recs:
        print(f"no evidence cached for {e.qualname}")
        print(f"  dig it:  memway dig {repo} {ref} --cache")
        return
    print(f"{e.coord_id}  {e.qualname}")
    print(f"  {len(recs)} records, current through "
          f"{(recs[0].get('dug_through_sha') or '')[:10]}\n")
    for r in recs:
        tag = r.get("short_sha") or f"#{r.get('number')}"
        print(f"  [{r.get('source')}] {tag}  {r.get('date')}  "
              f"{r.get('subject','')[:60]}")
        body = (r.get("body") or "").strip()
        if body:
            for line in body.splitlines()[:4]:
                print(f"      {line[:76]}")
        print()
def cmd_viz(repo, *args):
    """Render the map as a self-contained HTML explorer. Read-only."""
    from .viz import viz
    out, prefix, force = "", "", False
    rest = list(args)
    while rest:
        a = rest.pop(0)
        if a == "--out" and rest:
            out = rest.pop(0)
        elif a.startswith("--out="):
            out = a.split("=", 1)[1]
        elif a == "--filter" and rest:
            prefix = rest.pop(0)
        elif a.startswith("--filter="):
            prefix = a.split("=", 1)[1]
        elif a == "--force":
            force = True
        else:
            raise SystemExit(f"unknown flag {a!r} - "
                             f"use --out F, --filter PREFIX, --force")
    r = viz(repo, out, filter_prefix=prefix, force=force)
    if "error" in r:
        print(r["error"])
        if r.get("hint"):
            print(f"  {r['hint']}")
        sys.exit(1)
    print(f"wrote {r['out']}")
    print(f"  {r['line']}")
    if r["census"].get("boundary"):
        print(f"  {r['census']['boundary']} boundary nodes "
              f"(outside the filter, kept so edges are not silently cut)")


def cmd_console(repo, *args):
    """Serve the map live, with the read tools as buttons. 127.0.0.1 only,
    token-gated; the only write is a note at a coordinate."""
    from .console import serve
    port = 0
    rest = list(args)
    while rest:
        a = rest.pop(0)
        if a == "--port" and rest:
            port = int(rest.pop(0))
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
        else:
            raise SystemExit(f"unknown flag {a!r} - use --port N")
    httpd, url, _ = serve(repo, port=port)
    print(f"memway console on {url}")
    print("  127.0.0.1 only; the URL carries a single-session token")
    print("  read tools: summary show before_edit lineage dig")
    print("  the only write: a note at a coordinate")
    print("  ctrl-c to stop")
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()
        print("\nstopped")


COMMANDS = {
    "init": cmd_init, "index": cmd_index, "harvest": cmd_harvest,
    "show": cmd_show, "meta": cmd_meta, "lineage": cmd_lineage,
    "at": cmd_at, "setup": cmd_setup, "mcp": cmd_mcp,
    "dig": cmd_dig, "evidence": cmd_evidence,
    "viz": cmd_viz, "console": cmd_console,
    "pull": cmd_pull,
}


def _running_from_source() -> bool:
    """True when the imported package is a working tree, not an install.

    The third source of truth: when cwd is the repo root, a leftover
    `memway.egg-info` is discoverable by importlib.metadata and can win the
    lookup, so the SAME command reported different versions depending on
    where it was run from. Location does not lie - a package under
    site-packages was installed; anywhere else is a checkout.
    """
    here = Path(__file__).resolve().parent
    return not any(p in ("site-packages", "dist-packages") for p in here.parts)


def _is_editable(dist) -> bool:
    """Whether this distribution was installed with `pip install -e`."""
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        return False
    if not raw:
        return False
    try:
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except ValueError:
        return False


def _version() -> str:
    """The version to print. Metadata first - EXCEPT when editable.

    An editable install freezes its dist-info at `pip install -e` time and
    never updates again. This repo's own dev venv was wired at 0.49.2 and
    reported `memway 0.49.2` for weeks while running 0.50.1 source: the
    metadata was describing the install event, not the code. So for a
    WHEEL, metadata is authoritative (it is what was installed); for an
    EDITABLE install the source tree IS the install, and __version__ wins.

    Both checks matter. direct_url.json answers "was this pip install -e",
    and _running_from_source() covers the egg-info case where the editable
    dist-info is not even the distribution that got found.
    """
    from . import __version__ as source_version
    try:
        from importlib.metadata import distribution, PackageNotFoundError
    except ImportError:
        return source_version
    try:
        dist = distribution("memway")
    except PackageNotFoundError:
        return source_version
    except Exception:
        return source_version
    if _is_editable(dist) or _running_from_source():
        return source_version
    return dist.version or source_version


def _usage_line(cmd: str) -> str:
    """The command's own line from the module docstring - one source of
    truth for usage, so help and errors cannot drift."""
    for line in (__doc__ or "").splitlines():
        if line.strip().startswith(f"memway {cmd} ") or \
                line.strip() == f"memway {cmd}":
            return "usage: " + line.strip()
    return f"usage: memway {cmd} <repo> ... (see: memway --help)"


def main():
    import signal
    if hasattr(signal, "SIGPIPE"):          # D6
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = sys.argv[1:]
    # Before anything else: --version is the first thing a person types
    # after installing, and it must not fall through to the usage path
    # and exit non-zero.
    if args and args[0] in ("--version", "-V"):
        print(f"memway {_version()}")
        sys.exit(0)
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
                # same dash->underscore normalization the bool branch does:
                # no value flag has a dash today, but --max-age would map to
                # the kwarg 'max-age' and TypeError at bind() if it did not.
                key = name[2:].replace("-", "_")
                if "=" in a:
                    opts[key] = a.split("=", 1)[1]
                    args = args[:i] + args[i + 1:]
                elif i + 1 < len(args):
                    opts[key] = args[i + 1]
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
    fn = COMMANDS[args[0]]
    # Arity is checked BEFORE the call, with bind(), rather than by
    # catching TypeError around it - catching would also swallow a
    # TypeError raised deep inside a working command and report it as a
    # usage error, which is a worse lie than the traceback was.
    import inspect
    try:
        inspect.signature(fn).bind(*args[1:], **opts)
    except TypeError as e:
        sys.stderr.write(f"memway {args[0]}: {e}\n\n{_usage_line(args[0])}\n")
        sys.exit(2)
    fn(*args[1:], **opts)


if __name__ == "__main__":
    main()
