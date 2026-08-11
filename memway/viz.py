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

VIZ_WARN_ENTITIES = 1500
TEMPLATE = Path(__file__).with_name("viz_template.html")
PLACEHOLDER = "__MEMWAY_DATA__"
DEFAULT_OUT = "memway-map.html"


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
        "knowledge": [],
    }


def _knowledge_for(meta, e) -> list:
    """Decorated knowledge for one entity - via the STORE, never raw.

    read_all() is handed the entity's current hashes, which is what lets
    it mark an entry stale. Both tiers go in: an entry stamped with
    either the logic hash or the body hash is current.
    """
    md = meta.read_all(
        e.coord_id,
        current_hash={getattr(e, "logic_hash", ""), e.body_hash})
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

    scope = f" · {filter_prefix} subtree" if filter_prefix else ""
    return {
        "repo": f"{repo_p.name}{scope} · {len(rows)} entities / "
                f"{len(eds)} edges",
        "entities": rows,
        "edges": eds,
        "_census": {"entities": len(rows), "edges": len(eds),
                    "knowledge": kn_n, "stale": stale_n,
                    "boundary": len(boundary)},
    }


def render(payload: dict) -> str:
    """Inject the payload into the approved template."""
    html = TEMPLATE.read_text()
    if PLACEHOLDER not in html:
        raise RuntimeError(f"template lost its {PLACEHOLDER} placeholder")
    data = {k: v for k, v in payload.items() if not k.startswith("_")}
    # </script> inside a string would close the block early; JSON escapes
    # the slash so the browser never sees a literal closing tag.
    blob = json.dumps(data).replace("</", "<\\/")
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
