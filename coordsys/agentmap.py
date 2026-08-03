"""
Agent map: token-budgeted, confidence-honest scope queries over the
temporal code graph.

The core query an agent needs before touching code:
    scope(coordinate, budget_tokens) ->
        ranked entity cards for everything the change could reach,
        each carrying WHY it matters (impact x path confidence),
        HOW grounded the path is, and the attached rationale + staleness.

Design rules:
  - Path confidence = product of edge confidences along the best path.
    A blast radius through a bare-name edge is a guess and says so.
  - Ranking = impact (0.5^depth) x path_confidence x centrality prior.
  - The budget is a hard cap: cards are emitted best-first until spent,
    and the report states what fraction of total impact mass is shown.
  - Never pretend coverage: the grounding summary counts guessed edges.
"""
from collections import deque


def _rev_adj(edges):
    rev, emits, consumers = {}, {}, {}
    for e in edges:
        if e["kind"] in ("calls", "imports"):
            rev.setdefault(e["dst"], []).append(
                (e["src"], e.get("confidence", 0.9), e.get("resolution", "?")))
        elif e["kind"] == "emits":
            emits.setdefault(e["src"], []).append(e["dst"])
        elif e["kind"] == "consumes":
            consumers.setdefault(e["dst"], []).append(e["src"])
    return rev, emits, consumers


def centrality(edges, iters=20, d=0.85):
    """PageRank over call/import edges: a prior for which entities matter."""
    nodes, fwd = set(), {}
    for e in edges:
        if e["kind"] in ("calls", "imports"):
            nodes.add(e["src"]); nodes.add(e["dst"])
            fwd.setdefault(e["src"], []).append(e["dst"])
    if not nodes:
        return {}
    pr = {n: 1.0 / len(nodes) for n in nodes}
    for _ in range(iters):
        nxt = {n: (1 - d) / len(nodes) for n in nodes}
        for n, outs in fwd.items():
            share = d * pr[n] / len(outs)
            for o in outs:
                nxt[o] += share
        pr = nxt
    mx = max(pr.values())
    return {n: v / mx for n, v in pr.items()}


def scope(target_cid, edges, entities, meta=None, store=None,
          budget_tokens=1500, max_depth=4):
    """Confidence-propagating reverse reachability with ranked, budgeted cards."""
    rev, emits, consumers = _rev_adj(edges)
    # closure containment: a nested function is not independently
    # reachable - reaching it means reaching its enclosing function.
    closure_parent = {}
    for cid, e in entities.items():
        p = entities.get(getattr(e, "parent", None))
        if e.kind == "function" and p and p.kind in ("function", "method"):
            closure_parent[cid] = e.parent
    # best (confidence, -depth) path per reached node
    best = {}                        # cid -> (path_conf, depth, via)
    q = deque([(target_cid, 1.0, 0)])

    def reach(n, nc, d, how):
        if n == target_cid:
            return
        cur = best.get(n)
        if cur is None or nc > cur[0]:
            best[n] = (nc, d, how)
            q.append((n, nc, d))
            # propagate through enclosing functions at full confidence:
            # the closure and its owner are the same call site.
            p = closure_parent.get(n)
            while p:
                reach(p, nc, d, "closure")
                p = closure_parent.get(p)

    while q:
        cid, conf, d = q.popleft()
        if d >= max_depth:
            continue
        nbrs = [(s, c, how) for s, c, how in rev.get(cid, [])]
        for ev in emits.get(cid, []):
            for c in consumers.get(ev, []):
                nbrs.append((c, 0.5, "event"))
        for n, ec, how in nbrs:
            reach(n, conf * ec, d + 1, how)

    cen = centrality(edges)
    scored = []
    for cid, (pconf, depth, via) in best.items():
        e = entities.get(cid)
        if not e or e.kind not in ("function", "method", "class"):
            continue
        impact = 0.5 ** (depth - 1)
        rank = impact * pconf * (0.5 + 0.5 * cen.get(cid, 0.0))
        scored.append((rank, impact, pconf, depth, via, cid, e))
    scored.sort(key=lambda x: -x[0])

    total_mass = sum(s[1] for s in scored) or 1.0
    cards, spent, shown_mass = [], 0, 0.0
    guessed = sum(1 for s in scored if s[2] < 0.7)
    for rank, impact, pconf, depth, via, cid, e in scored:
        card = _card(cid, e, impact, pconf, depth, via, meta, store)
        cost = _tok(card)
        if spent + cost > budget_tokens:
            break
        cards.append(card); spent += cost; shown_mass += impact

    return {
        "target": target_cid,
        "affected_total": len(scored),
        "shown": len(cards),
        "impact_coverage": round(shown_mass / total_mass, 3),
        "grounding": {
            "fully_grounded_paths": sum(1 for s in scored if s[2] >= 0.9),
            "guessed_paths": guessed,
            "note": ("all paths static-resolved" if guessed == 0 else
                     f"{guessed} path(s) traverse low-confidence edges "
                     f"(bare-name/event resolution) - verify before trusting"),
        },
        "budget_tokens": budget_tokens,
        "spent_tokens": spent,
        "cards": cards,
    }


def _card(cid, e, impact, pconf, depth, via, meta, store):
    card = {
        "coord": cid,
        "qualname": e.qualname,
        "kind": e.kind,
        "sig": e.signature,
        "loc": e.loc,
        "depth": depth,
        "impact": round(impact, 3),
        "path_confidence": round(pconf, 2),
        "via": via,
    }
    if meta is not None:
        docs = meta.read_all(cid)
        notes = docs.get("notes", []) + docs.get("docs", [])
        if notes:
            latest = notes[-1]
            card["why"] = latest.get("text", "")[:160]
            if latest.get("stale"):
                card["why_stale"] = True
    if store is not None:
        anc = store.ancestry(cid)
        if anc:
            card["lineage"] = f'{anc[-1]["kind"]}<-{len(anc)} events'
    return card


def _tok(obj):
    """~4 chars/token estimate on the serialized card."""
    import json
    return max(1, len(json.dumps(obj)) // 4)
