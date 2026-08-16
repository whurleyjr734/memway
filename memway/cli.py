"""memway - a map of your codebase: coordinates, flow, and memory.

Quickstart:  pip install memway && memway setup .
             (builds the map, wires your agent, installs workflow rules)

Workflow: grep finds it; memway explains it and remembers it.

  memway setup [repo]                 one-command onboarding (see above)
  memway init <repo>                  build/refresh the map
  memway index <repo> [--if-stale]    re-index; --if-stale skips the work
                                        unless the tree moved [--quiet]
  memway hooks install [repo]         keep the map synced on commit,
                                        checkout and merge (uninstall to
                                        remove; never blocks a commit)
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
  memway summary <repo>               repo shape at a glance
  memway before-edit <repo> <ref>     the pre-change briefing
  memway verify-change [repo]         impact + what you staled
  memway attention <repo>             the queue: stale knowledge, comment
                                        rot, drifted design docs, markers
  memway mcp [repo]                   run the MCP server (agent wiring)
  memway --version                    print the installed version (-V)
  memway --json <q> <repo> [args]     structured: summary, at, show,
                                        before-edit, lineage, dig,
                                        attention, verify-change (which
                                        also names the knowledge your
                                        change just staled). All eight are
                                        reads: .coord is left untouched.

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


# .coord/.gitignore, written once by init. The DERIVED TIER TAXONOMY,
# expressed where git can act on it:
#
#   authored          meta/, lineage/   -> tracked, precious, never bulk-deleted
#   snapshot baseline docbindings.json  -> tracked; it is the ruler drift is
#                                          measured against
#   regenerable       cache/, evidence/ -> ignored; rebuilt from source
#   personal          log/, versions/   -> ignored; this machine's, not the team's
#
# Without it a user's repo tracks the pickle caches - measured on a fresh
# `memway init` + commit, which staged .coord/cache/*.pkl. Binary blobs
# that change on every index, conflict on every merge, and teach people
# that a dirty map is normal.
#
# It lives INSIDE .coord because that directory is memway's to manage.
# Editing the repo's root .gitignore would be the same trespass as
# rewriting somebody's CLAUDE.md or their git hook.
_COORD_GITIGNORE = """\
# written by memway init. Regenerable and personal tiers only - meta/,
# lineage/ and docbindings.json are TRACKED on purpose: clone the repo,
# inherit the knowledge.
cache/
evidence/
log/
versions/
"""


def cmd_init(repo):
    repo, coord, _ = _paths(repo)
    (coord / "index").mkdir(parents=True, exist_ok=True)
    gi = coord / ".gitignore"
    if not gi.exists():          # never clobber: it may have been edited
        gi.write_text(_COORD_GITIGNORE)
    (coord / "manifest.json").write_text(json.dumps({
        "format": "memway/0.1", "language": "python",
    }, indent=2))
    print(f"initialized {coord}")
    cmd_index(repo)


def cmd_index(repo, *flags):
    """Rebuild the map. `--if-stale` reindexes only when the tree moved.

    --if-stale runs on every commit once hooks are installed, so the
    CURRENT path must be fast and must not write: it reads the recorded
    sha, asks git for HEAD, and returns. Only the reindex writes, which is
    what keeps this under the read fence.
    """
    from . import freshness
    if_stale = "--if-stale" in flags
    quiet = "--quiet" in flags
    for f in flags:
        if f not in ("--if-stale", "--quiet"):
            raise SystemExit(f"memway index: unknown flag {f}\n\n"
                             f"{_usage_line('index')}")
    repo_p = Path(repo).resolve()
    if if_stale:
        coord_p = repo_p / ".coord"
        if not coord_p.exists():
            if not quiet:
                print(f"no map at {repo_p} - run memway init")
            return
        if not freshness.head_sha(repo_p):
            # hooks must never break a commit, and "not a git repo" is not
            # an error condition - it is just a place where we cannot tell.
            if not quiet:
                print("not a git repository - cannot tell if the map is stale")
            return
        gap = freshness.lag(repo_p, coord_p)
        if not gap:
            if not quiet:
                man = freshness.read_manifest(coord_p)
                at = man.get(freshness.SHA_KEY, "")
                print(f"map current at {at[:7]}" if at else "map current")
            return
        if not quiet:
            print(gap["message"].replace(" - run memway index",
                                         " - reindexing"))
        if quiet:
            # A hook that prints six lines on every commit gets uninstalled.
            # Silent when there was nothing to do, one line when there was.
            #
            # AND IT NEVER RAISES. This runs from post-commit; an exception
            # escaping here would print a traceback over somebody's commit
            # output and teach them to remove the hook. The failure goes to
            # .coord/log/hooks.log and the commit proceeds untouched.
            import io as _io
            import contextlib as _ctx
            import traceback as _tb
            _buf = _io.StringIO()
            try:
                with _ctx.redirect_stdout(_buf):
                    cmd_index(repo)
            except BaseException as exc:
                _log_hook_failure(coord_p, exc, _tb.format_exc())
                print(f"memway: reindex failed, see "
                      f"{coord_p / 'log' / 'hooks.log'} (commit unaffected)")
                return
            head = freshness.head_sha(Path(repo).resolve())
            print(f"memway: map reindexed at {head[:7]}")
            return
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
    # A map written before 0.54.0 holds sketches from the randomized
    # builtin hash. They cannot be recomputed - the old source text is
    # gone - so this one index runs without the minhash signal and SAYS
    # so, rather than scoring it 0.0 and reporting confident deletions
    # built on a measurement that never happened.
    migrating = getattr(ix, "stale_sketches", False)
    if migrating:
        print("  sketch generation changed (v1 -> v2): stored sketches came "
              "from a different hash")
        print("    this index runs WITHOUT the minhash signal; renames it "
              "cannot rule out are filed as pending-review")
        print("    lineage verdicts before and after this point are not "
              "comparable - see memway attention")
    lineage = detect_lineage(report, ix, store, meta,
                             use_sketch=not migrating)
    from .indexer import record_sketch_version
    record_sketch_version(coord)
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
    # last, so an index that died halfway does not claim a sha it never
    # finished describing.
    freshness.record(coord, repo)


def _warn_if_lagged(repo, coord, ix=None, meta=None):
    """One line when the map trails the tree, and one when knowledge has
    rotted. The always-on backstop for BOTH things that go stale.

    Hooks cover commit/checkout/merge. This covers bisect, worktrees,
    hand-edited trees and every repo where nobody installed anything. The
    map may lag; it must not lag silently.

    The knowledge line exists because the same promise was never made for
    the other half. `show <ref>` flagged a stale entry only if you already
    suspected that coordinate; nothing said anything repo-wide. 0.54.1
    shipped a rule telling people to supersede what they staled, and the
    author broke it twice in one evening with the tool installed, because
    nothing ever told them which coordinates had gone stale. Ambient
    beats a rule you have to remember.

    ix/meta optional: callers that already loaded them pass them, and
    nobody pays for a second load just to be warned.
    """
    from . import freshness
    try:
        gap = freshness.lag(repo, coord)
    except Exception:
        return
    if gap:
        print(f"  note: {gap['message']}")
    if ix is not None and meta is not None:
        from . import query
        kn = query._knowledge_lag(ix, meta)
        if kn:
            print(f"  note: {kn['message']}")


def _unresolved(ref, ix) -> None:
    """Print why a ref did not resolve, then exit nonzero.

    ONE printer for both commands. They had a copy each, both saying
    "no entity matches" even when several entities did - which is how a
    caller concludes the map is ignorant and reaches for grep instead.
    Exiting 0 on a failed lookup was the other half: a script could not
    tell a miss from a hit.
    """
    from . import query
    err = query._resolve_error(str(ref), ix)
    print(err["error"])
    for qn in err.get("matches", []):
        print(f"  {qn}")
    if err.get("closest"):
        print(f"  closest: {', '.join(err['closest'])}")
    if err.get("hint"):
        print(f"  {err['hint']}")
    sys.exit(1)


def cmd_show(repo, ref):
    repo, coord, ix, edges, meta, reg = _load(repo)
    _warn_if_lagged(repo, coord, ix, meta)
    e = ix.resolve(ref)
    if not e:
        _unresolved(ref, ix)
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
        _unresolved(ref, ix)
    from .metadata import GhostEntity
    try:
        stamp = stamp_for(e, repo)
    except GhostEntity as exc:
        print(str(exc)); sys.exit(1)
    meta.add(e.coord_id, channel, text, author=author, body_hash=stamp)
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
# THE RULES ARE EMITTED TO THREE FILENAMES, ONE TEMPLATE
# ======================================================
#
# AGENTS.md is canonical; CLAUDE.md and GEMINI.md are byte-identical
# copies written in the same pass. Not redundancy for its own sake: a
# client reads the filename it knows and ignores the rest, so a repo with
# only CLAUDE.md silently gives non-Claude agents no rules at all. They
# then work correctly and record nothing, which is the exact failure the
# rules exist to prevent and is invisible while it happens.
#
# One template, three writes, and a test asserting the managed blocks are
# identical - so drift between them is structurally impossible rather
# than merely unlikely.
RULE_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")

# The managed region. Everything after the end marker belongs to the user
# and survives an upgrade; everything between the markers is ours and does
# not. A file with no markers is not assumed to be ours.
_RULES_BEGIN = "<!-- memway:rules v1 -->"
_RULES_END = ("<!-- /memway:rules - everything below this line is yours; "
              "memway setup preserves it. -->")

# Tool names are EXACT and CLI equivalents are named, because an agent that
# has to guess a name skips the call (finding #14), and an agent with no
# MCP client has no way to act on a rule written only in MCP terms.
_AGENT_RULES_BODY = """\
# Project rules

This repo uses memway as its memory layer. Each tool below is available
over MCP with the exact name given; where a CLI equivalent exists it is
named in parentheses. Use whichever your client supports.

- Before editing any function or class, brief yourself on it with
  `memway_before_edit` (CLI: `memway --json before-edit . <ref>`) and heed
  any attached knowledge. If the call returns an error, do NOT edit -
  resolve the ref first with `memway_at` (CLI: `memway at . <file:line>`),
  or try a bare function name or module.qualname, then retry.
- After completing changes, call `memway_verify_change`
  (CLI: `memway --json verify-change .`) to confirm impact. It reports
  which tests reach your change through the graph; running them is your
  job, not its.
- When a task, design doc, or conversation supplies a REASON a piece of
  code is the way it is (constraints, incidents, tuning rationale), record
  that reason with `memway_meta` (CLI: `memway meta . <ref> notes "<text>"`)
  on the relevant entity - reasons must outlive this session. This is due
  whenever a reason or finding SURFACES, not only when a change lands:
  tasks you decline, block on, investigate, or leave unfinished count too.
  The reason a change was refused is often the most valuable thing to
  record - a constraint strong enough to stop work is exactly what the next
  session needs and exactly what the code cannot say on its own. Capture it
  before you reply.
- When you are told knowledge has gone stale, supersede it before you
  finish. You will be told without asking: every read reports
  "N coordinates hold stale knowledge", and `memway_verify_change`
  (CLI: `memway verify-change .`) names exactly which ones your change
  invalidated. Write a fresh entry in the SAME channel - a confirm does
  not answer a stale note. Superseding never deletes; the old entry stays
  as history, and only the newest entry per channel decides.
"""


def rules_document(tail: str = "") -> str:
    """The full managed document, optionally with a user tail appended."""
    doc = f"{_RULES_BEGIN}\n{_AGENT_RULES_BODY}{_RULES_END}\n"
    return doc.rstrip("\n") + tail if tail else doc


def managed_block(text: str) -> str:
    """The memway-managed region of a rules file, or '' if unmarked."""
    if _RULES_BEGIN not in text or _RULES_END not in text:
        return ""
    start = text.index(_RULES_BEGIN)
    end = text.index(_RULES_END) + len(_RULES_END)
    return text[start:end]


# Bodies emitted by earlier versions, which predate the markers. A file
# matching one exactly is provably ours and provably unedited, so it can be
# upgraded whole. Anything else unmarked is ambiguous and is refused.
_LEGACY_RULES = ("""\
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
""",)


def plan_rules_write(path: Path, doc: str) -> tuple:
    """(content_or_None, message) for one rules file. Never clobbers.

    Four cases, and the fourth is the one that matters: a file we cannot
    prove is ours is left alone and reported, because silently rewriting
    somebody's project rules is worse than doing nothing.
    """
    if not path.exists():
        return doc, f"wrote {path.name}"
    cur = path.read_text()
    if _RULES_BEGIN in cur and _RULES_END in cur:
        tail = cur[cur.index(_RULES_END) + len(_RULES_END):]
        kept = " (kept your additions below the marker)" if tail.strip() else ""
        return rules_document(tail), f"updated {path.name}{kept}"
    if cur in _LEGACY_RULES or cur.strip() in [l.strip() for l in _LEGACY_RULES]:
        return doc, f"upgraded {path.name} (older memway rules, unedited)"
    return None, (f"{path.name} exists and carries no memway marker - "
                  f"REFUSING to touch it (add {_RULES_BEGIN} above your "
                  f"content to opt in)")

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
    # One template, three filenames, one pass. A client reads the name it
    # knows, so emitting only CLAUDE.md leaves every other agent ruleless.
    doc = rules_document()
    for name in RULE_FILES:
        content, msg = plan_rules_write(repo_p / name, doc)
        if content is not None:
            (repo_p / name).write_text(content)
        print(msg)
    print("\ntip: memway hooks install keeps the map synced on "
          "commit/checkout/merge")
    print("\nnext steps:")
    print("  1. restart your agent in this directory "
          "(it will pick up .mcp.json)")
    print('  2. ask it: "what does this repo know?"')


def _log_hook_failure(coord, exc, tb: str) -> None:
    """One line to .coord/log/hooks.log. Best effort, never raises."""
    import time
    try:
        d = Path(coord) / "log"
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(d / "hooks.log", "a") as fh:
            fh.write(f"{stamp} index --if-stale failed: "
                     f"{type(exc).__name__}: {exc}\n")
            fh.write("".join(f"    {l}\n" for l in tb.strip().splitlines()[-3:]))
    except Exception:
        pass


def cmd_hooks(action, repo="."):
    """Install or remove git hooks that keep the map in step.

    Opt-in by design: `setup` advertises this and never runs it. A tool
    that writes into .git/hooks uninvited has taken something it was not
    offered.
    """
    from . import hooks as _h
    if action not in ("install", "uninstall"):
        raise SystemExit(f"memway hooks: unknown action {action!r}\n\n"
                         f"{_usage_line('hooks')}")
    fn = _h.install if action == "install" else _h.uninstall
    for line in fn(Path(repo).resolve()):
        print(line)
    if action == "install":
        print()
        for line in _h.describe():
            print(line)
        print("  a failure never blocks the git operation")


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
def cmd_summary(repo):
    """Repo shape at a glance. The CLI door for the summary query."""
    from . import query
    r = query.summary(repo)
    if "error" in r:
        print(r["error"]); sys.exit(1)
    for k in ("map_lag", "knowledge_lag"):
        if r.get(k):
            print(f"  note: {r[k]['message']}")
    ents = r.get("entities") or r.get("entity_count")
    print(f"{r.get('repo', repo)}")
    for key in ("languages", "entities", "edges", "knowledge", "hardest"):
        if key in r and not isinstance(r[key], (dict, list)):
            print(f"  {key}: {r[key]}")
    kn = r.get("knowledge") or {}
    if isinstance(kn, dict):
        print(f"  knowledge: {kn.get('total_entries', 0)} entries across "
              f"{kn.get('coordinates_with_knowledge', 0)} coordinates")


def cmd_before_edit(repo, ref):
    """The pre-change briefing. The CLI door for before_edit."""
    from . import query
    r = query.before_edit(repo, ref)
    if "error" in r:
        print(r["error"])
        for c in r.get("closest", []) or r.get("matches", []):
            print(f"  {c}")
        sys.exit(1)
    for k in ("map_lag", "knowledge_lag"):
        if r.get(k):
            print(f"  note: {r[k]['message']}")
    e = r.get("entity", {})
    print(f"{e.get('coord_id')}  {e.get('qualname')}")
    print(f"  at {e.get('path')}:{e.get('line')}")
    m = r.get("metrics", {})
    print(f"  complexity {m.get('complexity')}  callers "
          f"{len(r.get('direct_callers', []))}  blast "
          f"{r.get('downstream', {}).get('downstream_count')}")
    for k in r.get("knowledge", []):
        flag = " [STALE]" if k.get("stale") else ""
        print(f"  [{k['channel']}]{flag} {k['text'][:100]}")
    for w in r.get("warnings", []):
        print(f"  ! {w}")


def cmd_verify_change(repo="."):
    """Post-change impact AND the knowledge this change staled.

    The CLI door. It existed over MCP and --json only, which is why the
    pre-commit hook had nothing readable to call - and why the author of
    the supersede-before-you-finish rule broke it twice in one evening.
    """
    from . import query
    r = query.verify_change(repo)
    if "error" in r:
        print(r["error"]); sys.exit(1)
    changed = r.get("changed") or []
    print(f"changed: {len(changed)}" + (f" - {', '.join(changed[:6])}" if changed else ""))
    print(f"  impacted: {r.get('impacted', 0)}")
    t = r.get("tests", {})
    print(f"  tests reached: {len(t.get('grounded', []))} grounded, "
          f"{len(t.get('name_hit', []))} by name")
    staled = r.get("staled_knowledge") or []
    if staled:
        print(f"  STALED KNOWLEDGE ({len(staled)}) - supersede in the SAME channel:")
        for e in staled:
            print(f"    {e['coordinate']}  {e['qualname']}  [{e['channel']}]")
            print(f"      {e['text'][:120]}")
    else:
        print("  staled knowledge: none")


def cmd_attention(repo):
    """The repo's attention queue: everything flagged as needing eyes.

    MCP-only until 0.54.1. It was not a --json query and not a command, so
    anyone driving memway from a terminal - which is how this project's own
    releases are built - had no way to ask what had gone stale.
    """
    from . import query
    r = query.attention(repo)
    if "error" in r:
        print(r["error"])
        sys.exit(1)
    rot = r.get("comment_rot") or []
    docs = r.get("stale_design_docs") or []
    marks = r.get("markers") or []
    print(f"stale knowledge entries: {r.get('stale_notes', 0)}")
    print(f"comment rot: {len(rot)}   design docs drifted: {len(docs)}   "
          f"markers: {r.get('marker_total', 0)}")
    # These lists carry plain qualname strings, not dicts. Written as
    # dicts first, which printed a traceback on the very first run - the
    # shape is worth reading rather than assuming.
    def _label(x):
        return x if isinstance(x, str) else (
            x.get("qualname") or x.get("doc") or x.get("coord") or str(x))
    for item in rot[:15]:
        print(f"  rot   {_label(item)}")
    if len(rot) > 15:
        print(f"  ... and {len(rot) - 15} more")
    for d in docs[:10]:
        print(f"  doc   {_label(d)}")
    for m in marks[:10]:
        print(f"  mark  {_label(m)}")


def cmd_viz(repo, *args, force=False):
    """Render the map as a self-contained HTML explorer. Read-only.

    `force` arrives as a keyword because main() lifts declared flags out
    of argv before dispatch; the --force branch below stays for a direct
    call that passes it positionally.
    """
    from .viz import viz
    out, prefix = "", ""
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
    # flush=True, and it matters. Python block-buffers stdout when it is
    # not a TTY, and this process then blocks in serve_forever() without
    # ever flushing - so `memway console > log` produced ZERO bytes while
    # the server was up and listening, and the single-session token, which
    # exists nowhere else, was unobtainable. Measured in the 0.54.0
    # acceptance: the banner only appeared under `python -u`.
    print(f"memway console on {url}", flush=True)
    print("  127.0.0.1 only; the URL carries a single-session token",
          flush=True)
    print("  read tools: summary show before_edit lineage dig", flush=True)
    print("  the only write: a note at a coordinate", flush=True)
    print("  ctrl-c to stop", flush=True)
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
    "dig": cmd_dig, "evidence": cmd_evidence, "hooks": cmd_hooks,
    "viz": cmd_viz, "console": cmd_console,
    "pull": cmd_pull, "attention": cmd_attention,
    # The three read doors that existed over MCP and --json but not here.
    # verify-change is the one that mattered: the pre-commit hook had
    # nothing readable to call.
    "summary": cmd_summary, "before-edit": cmd_before_edit,
    "verify-change": cmd_verify_change,
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


# Flags lifted out of argv before dispatch, each declaring which commands
# may receive it. Module level so a test can read them - and one does:
# test_every_documented_flag_is_receivable walks the usage text above and
# checks every flag it advertises against this table and against the
# command's own parser.
#
# A FLAG CAN BELONG TO MORE THAN ONE COMMAND. This mapped one owner each
# for a year, and `memway viz --force` - a flag printed in viz's own usage
# line - was rejected with "applies to 'pull' only", because pull happened
# to claim --force first. The usage text and the parser disagreed, and the
# usage text was right.
VALUE_FLAGS = {"--author": ("meta",), "--source": ("pull",),
               "--into": ("pull",)}
BOOL_FLAGS = {"--force": ("pull", "viz"), "--replace-meta": ("pull",)}


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
    wrong = [f for f, own in owners.items() if args[0] not in own]
    if wrong:
        who = owners[sorted(wrong)[0]]
        sys.stderr.write(f"{', '.join(sorted(wrong))} applies to "
                         f"{' or '.join(repr(c) for c in who)} only\n")
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
