"""
Viz: the real map as a self-contained interactive HTML explorer.

READ TOOL. Like `dig`, this never writes to .coord - a test asserts the
map is byte-identical after a run. The output goes to the REPO ROOT
(default ./memway-map.html), never inside .coord, because it is a
rendering of the map and not part of it.

Zero new runtime dependencies: stdlib only. D3 stays a CDN reference in
the template, which is the one thing the page needs a network for.

KNOWLEDGE COMES THROUGH THE STORE'S READ PATH
=============================================

Not raw JSONL reads. `MetaStore.read_all(cid, current_hash=...)` is what
decorates each entry with `stale`, and staleness is the whole point: a
note rendered without its flag asserts a currency the map never claimed.
Reading the files directly would produce a page that looks identical and
lies. Channel names, historical markers and aggregate markers ride the
same path.

SCALE HONESTY
=============

Above VIZ_WARN_ENTITIES the command refuses and asks for `--force` or
`--filter <prefix>`. It never silently samples: a map that quietly drops
half the repo is worse than one that refuses, because the reader cannot
tell the difference between "absent" and "not rendered". `--filter`
renders a subtree PLUS its direct out-of-subtree neighbours, and marks
those neighbours as boundary nodes so the edge of the view is visible.
"""

import json
import re
from pathlib import Path

from .verify import is_test_entity

VIZ_WARN_ENTITIES = 1500
TEMPLATE = Path(__file__).with_name("viz_template.html")
PLACEHOLDER = "__MEMWAY_DATA__"
TITLE_SLOT = "__MEMWAY_TITLE__"
DEFAULT_OUT = "memway-map.html"

VENDOR = Path(__file__).with_name("vendor")
D3 = VENDOR / "d3.min.js"
D3_SLOT = "/* AIRGAP_D3 */"


def load_template() -> str:
    """The template with every external asset inlined. THE one reader.

    There are two render paths - viz writes a file, the console serves a
    page - and both must emit HTML that makes ZERO network requests. That
    guarantee is asserted on the emitted bytes of both in
    tests/test_airgap.py, and the way it would rot is a second
    `TEMPLATE.read_text()` growing somewhere that forgets to inline. So
    there is one reader and both paths call it.

    d3 is inlined rather than linked because a rendered map is a picture of
    somebody's private source tree; a page that fetches a script is a page
    that announces the repo exists to a CDN, and fails outright on a plane
    or behind a proxy. ~273KB is the honest price.
    """
    html = TEMPLATE.read_text()
    if D3_SLOT not in html:
        raise RuntimeError(f"template lost its {D3_SLOT} slot")
    d3 = D3.read_text(encoding="utf-8")
    # A literal </script> in the payload would close the block early. d3's
    # minified build contains no `</` at all, but that is d3's property, not
    # ours, and a future bump could change it.
    if "</" in d3:
        raise RuntimeError("vendored d3 contains '</' and would break out "
                           "of its script block - escape it before inlining")
    return html.replace(D3_SLOT, d3)


def _entity_row(e) -> dict:
    """One entity in the template's shape.

    The template speaks `id`/`file`/`lines`, not `coord_id`/`path`/
    `lineno` - it was written to eat hand-made JSON too. Mapping here
    keeps that contract rather than rewriting the approved page.
    """
    start = getattr(e, "lineno", 0) or 0
    end = getattr(e, "end_lineno", 0) or start
    return {
        "id": e.coord_id,
        "qualname": e.qualname,
        "kind": (e.kind or "function").lower(),
        "file": e.path,
        "lines": f"{start}-{end}",
        "complexity": int(getattr(e, "complexity", 1) or 1),
        # Presentation only. The same test/source rule the summary uses
        # (verify.is_test_entity: path and filename, never the qualname),
        # so the two views cannot disagree about the same repo. No metric
        # is read or written here.
        "is_test": is_test_entity(e),
        "knowledge": [],
    }


def _knowledge_for(meta, e) -> list:
    """Decorated knowledge for one entity - via the STORE, never raw.

    read_all() is handed the entity's current hashes, which is what lets
    it mark an entry stale. Both tiers go in: an entry stamped with
    either the logic hash or the body hash is current.
    """
    from .metadata import accepted_for
    md = meta.read_all(e.coord_id, current_hash=accepted_for(e))
    out = []
    for channel, entries in md.items():
        for en in entries:
            row = {
                "channel": channel,
                "text": en.get("text", ""),
                "stale": bool(en.get("stale")),
                "author": en.get("author", ""),
            }
            # Markers the excavated channel adds. Carried so the card can
            # say "this describes code as of a commit" rather than
            # implying it describes the code now.
            if en.get("historical"):
                row["historical"] = True
            if en.get("aggregate"):
                row["aggregate"] = True
                row["entity_count"] = en.get("entity_count", 0)
            out.append(row)
    return out


def _subtree(entities, prefix: str):
    """(in-subtree ids, boundary ids) for a qualname prefix.

    Boundary = an entity outside the subtree that a subtree entity
    touches directly. Rendering the subtree alone would silently cut its
    edges; rendering the neighbours unmarked would misrepresent the
    scope. So they are included AND labelled.
    """
    inside = {cid for cid, e in entities.items()
              if e.qualname == prefix or e.qualname.startswith(prefix + ".")}
    return inside


def has_unsuperseded_stale(knowledge: list) -> bool:
    """Does this coordinate hold stale knowledge nobody has answered yet?

    Delegates to metadata.unsuperseded_stale - the ring and verify_change's
    report must never be able to disagree about what "stale" means, and
    two copies of this rule is exactly how they would.
    """
    from .metadata import unsuperseded_stale
    return bool(unsuperseded_stale(knowledge))


def export(repo: str, *, filter_prefix: str = "", force: bool = False) -> dict:
    """Build the render payload. Reads only; writes nothing."""
    from .indexer import Indexer
    from .edges import EdgeBuilder
    from .metadata import MetaStore

    repo_p = Path(repo).resolve()
    coord = repo_p / ".coord"
    if not (coord / "index" / "coordinates.json").exists():
        return {"error": f"no map at {coord} - run `memway init {repo}` first"}

    ix = Indexer(repo_p, coord)
    # write_cache=False is the fence: load_existing otherwise warms
    # .coord/cache/coordinates.pkl, and a read tool that writes is not a
    # read tool (same lesson as memway dig).
    ix.load_existing(write_cache=False)
    # write_cache=False here too: edges.json has its own pickle,
    # and the fence is only as strong as its leakiest loader.
    edges_all = EdgeBuilder.load(coord, write_cache=False)
    meta = MetaStore(coord)

    ents = dict(ix.entities)
    boundary: set = set()
    if filter_prefix:
        inside = _subtree(ents, filter_prefix)
        if not inside:
            near = sorted({q.split(".")[0] for q in ix.by_qualname})[:6]
            return {"error": f"no entities under prefix {filter_prefix!r}",
                    "hint": f"top-level prefixes include: {', '.join(near)}"}
        for ed in edges_all:
            s, d = ed.get("src"), ed.get("dst")
            if s in inside and d in ents and d not in inside:
                boundary.add(d)
            elif d in inside and s in ents and s not in inside:
                boundary.add(s)
        keep = inside | boundary
        ents = {cid: e for cid, e in ents.items() if cid in keep}

    if len(ents) > VIZ_WARN_ENTITIES and not force and not filter_prefix:
        return {
            "error": f"{len(ents)} entities exceeds the {VIZ_WARN_ENTITIES} "
                     f"readable limit for a force-directed graph",
            "hint": "re-run with --filter <qualname-prefix> to render a "
                    "subtree (plus its direct neighbours, marked as "
                    "boundary), or --force to render everything anyway. "
                    "Nothing is ever sampled silently.",
            "entities": len(ents),
        }

    rows, stale_n, kn_n = [], 0, 0
    for cid, e in sorted(ents.items(), key=lambda kv: kv[1].qualname):
        row = _entity_row(e)
        kn = _knowledge_for(meta, e)
        row["knowledge_stale"] = has_unsuperseded_stale(kn)
        row["knowledge"] = kn
        kn_n += len(kn)
        stale_n += sum(1 for k in kn if k["stale"])
        if cid in boundary:
            row["boundary"] = True
            row["qualname"] = row["qualname"] + "  [boundary]"
        rows.append(row)

    ids = set(ents)
    eds = [{"source": ed["src"], "target": ed["dst"],
            "kind": ed.get("kind", "calls"),
            "confidence": ed.get("confidence", 1.0)}
           for ed in edges_all
           if ed.get("src") in ids and ed.get("dst") in ids]

    return {
        "repo": map_label(repo_p, filter_prefix, len(rows), len(eds)),
        "entities": rows,
        "edges": eds,
        "_census": {"entities": len(rows), "edges": len(eds),
                    "knowledge": kn_n, "stale": stale_n,
                    "boundary": len(boundary)},
    }


def project_name(repo_p) -> str:
    """What this project is CALLED, in precedence order.

    THE DECISION, recorded here rather than in a commit message:

      1. pyproject.toml [project].name
      2. package.json   name
      3. git remote origin basename
      4. the directory name

    Rationale, in that order: a declared package name is the project's own
    statement of identity and the only one it maintains deliberately. A
    remote basename is next because it is chosen but can be renamed
    underneath you. The directory is LAST because it is an accident of
    whoever cloned it - memway's own checkout is called "coordsys-v49",
    the pre-rename name, and so the flagship map published under it for
    weeks while every other surface said memway.

    FIRST IN CHAIN WINS, including when pyproject and package.json
    disagree - a polyglot repo with both is not a tie to be resolved by
    cleverness, it is a repo whose Python packaging is authoritative here
    because that is what memway is distributed as. Deterministic beats
    clever; a rule you can predict is worth more than one that is right
    slightly more often.

    Never raises. Every tier is best-effort and falls through on any
    error - a malformed pyproject must not stop a map from rendering.
    """
    from pathlib import Path as _P
    repo_p = _P(repo_p)
    try:
        import tomllib
        f = repo_p / "pyproject.toml"
        if f.is_file():
            name = tomllib.loads(f.read_text()).get("project", {}).get("name")
            if name:
                return str(name)
    except Exception:
        pass
    try:
        import json as _json
        f = repo_p / "package.json"
        if f.is_file():
            name = _json.loads(f.read_text()).get("name")
            if name:
                return str(name)
    except Exception:
        pass
    try:
        import subprocess
        r = subprocess.run(["git", "-C", str(repo_p), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
        url = r.stdout.strip()
        if r.returncode == 0 and url:
            base = url.rstrip("/").rsplit("/", 1)[-1]
            if base.endswith(".git"):
                base = base[:-4]
            if base:
                return base
    except Exception:
        pass
    return repo_p.name


def map_label(repo_p, filter_prefix: str, n_entities: int,
              n_edges: int) -> str:
    """THE label for a rendered map. Header and browser tab both use it.

    They used to be independent: the header was built here and the title
    was a constant in the template reading "memway - itsdangerous, the
    real map" - a leftover from when the flagship map really was
    itsdangerous. Every map every user generated inherited it, so their
    tab announced somebody else's project (C-b93d8e). Nothing caught it
    because a wrong constant is not a wrong behaviour: the payload, airgap
    and executed-predicate tests all pass on a page whose tab lies.

    One function, called once, used twice. The point is not the string -
    it is that the two can no longer drift apart.

    NOTE: repo_p.name is the DIRECTORY, so this repo currently labels
    itself "coordsys-v49". Wrong, and fixed at this one source in 0.54.2
    rather than papered over here.
    """
    scope = f" · {filter_prefix} subtree" if filter_prefix else ""
    return (f"{project_name(repo_p)}{scope} · {n_entities} entities / "
            f"{n_edges} edges")


def render(payload: dict) -> str:
    """Inject the payload into the approved template."""
    html = load_template()
    if PLACEHOLDER not in html:
        raise RuntimeError(f"template lost its {PLACEHOLDER} placeholder")
    data = {k: v for k, v in payload.items() if not k.startswith("_")}
    # </script> inside a string would close the block early; JSON escapes
    # the slash so the browser never sees a literal closing tag.
    blob = json.dumps(data).replace("</", "<\\/")
    # The tab and the header now read from ONE derivation. No guard
    # clause here on purpose: if the slot ever goes missing the
    # literal survives into the title and the test fails loudly,
    # which is the same protection with less machinery.
    html = html.replace(TITLE_SLOT, payload.get("repo", "map"))
    return html.replace(PLACEHOLDER, blob)


def viz(repo: str, out: str = "", *, filter_prefix: str = "",
        force: bool = False) -> dict:
    payload = export(repo, filter_prefix=filter_prefix, force=force)
    if "error" in payload:
        return payload
    dest = Path(out) if out else Path(repo).resolve() / DEFAULT_OUT
    dest.write_text(render(payload))
    c = payload["_census"]
    return {"out": str(dest), "census": c,
            "line": f"{c['entities']} entities / {c['edges']} edges / "
                    f"{c['knowledge']} knowledge entries / "
                    f"{c['stale']} stale"}
