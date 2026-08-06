"""
Blast radius: given changed entities, how far does the risk reach?

Walks the graph BACKWARDS (who calls/imports the changed thing,
transitively) and FORWARDS through events (who consumes what the
changed thing emits - event-driven blast crosses language boundaries).

Each affected coordinate gets an impact score decayed by distance:
    impact = 0.5 ** (depth - 1)
so direct callers count 1.0, their callers 0.5, and so on. An entity
reachable by several paths keeps its highest score (shortest path
dominates). Total radius = sum of impacts - a single number to compare
the risk of two candidate changes.

Depth is capped (default 4): beyond that, everything in a connected
codebase reaches everything, and the number stops meaning anything.
"""

from collections import deque


def blast_radius(targets: list[str], edges: list[dict],
                 max_depth: int = 4) -> dict:
    """targets: coordinate IDs of changed entities."""
    # reverse adjacency: dst -> [srcs] for calls/imports;
    # event propagation: emitter -> consumers of the same event
    rev: dict[str, list[str]] = {}
    emits: dict[str, list[str]] = {}      # coord -> event names
    consumers: dict[str, list[str]] = {}  # event name -> coords
    for e in edges:
        if e["kind"] in ("calls", "imports"):
            rev.setdefault(e["dst"], []).append(e["src"])
        elif e["kind"] == "emits":
            emits.setdefault(e["src"], []).append(e["dst"])
        elif e["kind"] == "consumes":
            consumers.setdefault(e["dst"], []).append(e["src"])

    best_depth: dict[str, int] = {}
    q = deque((t, 0) for t in targets)
    seen = set(targets)
    via_event = set()
    while q:
        cid, d = q.popleft()
        if d >= max_depth:
            continue
        nxt = list(rev.get(cid, []))
        for ev in emits.get(cid, []):
            for c in consumers.get(ev, []):
                if c not in seen:
                    via_event.add(c)
                nxt.append(c)
        for n in nxt:
            if n in seen:
                continue
            seen.add(n)
            best_depth[n] = d + 1
            q.append((n, d + 1))

    impacts = {cid: 0.5 ** (d - 1) for cid, d in best_depth.items()}
    # dynamic events make the radius a LOWER BOUND: if any reached
    # entity emits an event whose name is unknowable statically, the
    # true blast may extend further than this graph shows.
    dynamic_emitters = [c for c in (set(impacts) | set(targets))
                        if "EVT:<dynamic>" in emits.get(c, [])]
    return {
        "dynamic_emitters": dynamic_emitters,
        "radius_is_lower_bound": bool(dynamic_emitters),
        "targets": targets,
        "affected": impacts,                       # coord -> impact
        "depths": best_depth,                      # coord -> distance
        "via_event": via_event & set(impacts),     # crossed an event edge
        "radius": round(sum(impacts.values()), 2), # single risk number
    }
