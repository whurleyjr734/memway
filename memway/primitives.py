"""Graph primitives over the coordinate map. NOT about memory.

WHY THIS MODULE EXISTS. Every tool memway shipped until 0.63 answered a
question about accumulated KNOWLEDGE - what do we know about this
coordinate, what went stale, who attested to it. That is the product, and
it has a cold-start problem that no amount of polish fixes: a map indexed
five minutes ago holds zero notes, so every one of those tools returns
nothing useful until somebody has been recording for weeks. The cost of
the workflow is immediate and the benefit is deferred, which is exactly
the shape that makes a tool get uninstalled.

But the substrate underneath is not a memory store. It is a code graph
with per-entity structure hashes, minhash sketches, resolved edges
carrying confidence, and a test lens. Most of that was computed for
internal use and never had a door: `shape_hash` and `sketch` are written
for every entity in every repo, shipped inside every published bundle,
and existed solely to help lineage match renames. Nobody could ask for
them.

So these are primitives, and they work on a map with no knowledge in it
at all.

THE RULE FOR ADDING ONE: it must answer something grep and Read cannot.
Otherwise the agent should just grep, and a tool that duplicates grep is
a worse LSP plus a context-budget tax. `clones` passes because
shape_hash is name-insensitive - two identical bodies under different
names are invisible to any text search. `covering_tests` passes because its
grounded tier comes from traced edges; grep can only ever produce the
name-hit tier, which this labels as the guess it is.
"""
from __future__ import annotations

import collections

from .payload import rank_bound_report

# DERIVED FROM MEASUREMENT, not taste. shape_hash on a tiny body is
# meaningless - every one-line delegator in a repo collides - so a floor
# is required, and the question is where. Measured on memway's own map
# (1,074 callables, 15 shape groups with more than one member):
#
#   loc>=0   15 groups / 45 members     loc>=5    3 groups / 6 members
#   loc>=3   14 groups / 42 members     loc>=8    2 groups / 4 members
#
# The floor that matters is between 3 and 5, and 5 is wrong: it discards
# the 7-member `_cli` group and the 6-member `_git` group, which are the
# genuine finding here - the same subprocess helper copied into seven test
# files under two different names. A floor that hides the true positives
# to avoid the trivial ones has optimised for a quiet report.
MIN_CLONE_LOC = 3

# Above this, two bodies are near-duplicates worth looking at. Reported as
# a SCORE, never as a fact: the sketch is a minhash estimate, and the one
# thing this project refuses to do is give a measurement the authority of
# a hash match.
NEAR_DEFAULT = 0.80

# Members shown per clone group. Smaller than payload.CAP on purpose: a
# group is already a summary, and its COUNT is what a reader acts on. Five
# names are enough to go and look; the twelfth is not worth the budget
# when there are twelve groups of them.
MEMBERS_CAP = 5


def _origin(members) -> str:
    """Where a clone group lives: production, test-only, or mixed.

    THROUGH is_test_entity, which is THE one test/source rule - summary,
    viz and the test lens all join there rather than each deciding for
    itself, and a second rule is how two views start disagreeing about the
    same repo.

    MIXED EARNS ITS OWN BUCKET rather than being folded into either side.
    "The same body exists in production and in a test" is a different
    finding from duplication within one of them - sometimes a test
    reimplementing logic instead of calling it. Honesty about its size:
    on pydantic it is 4 groups of 471, and at three lines they look
    coincidental (a test's __init__ matching NameEmail.__init__ is two
    short constructors, not a smell). It is a real category carrying
    little weight here, which is worth knowing before anyone builds on it.

    CLASSIFIED BY PATH, because is_test_entity is - deliberately, since a
    function called test_connection in production code is production code.
    The consequence runs the other way too and is not hidden: a genuine
    helper living under tests/ is called a test, and a vendored subtree
    like pydantic-core counts as production. That is a claim about
    LOCATION, which is why the split is reported as a census the reader
    can weigh rather than applied as a filter they cannot see.
    """
    from .verify import is_test_entity
    n = sum(1 for e in members if is_test_entity(e))
    if n == 0:
        return "production"
    return "test-only" if n == len(members) else "mixed"


def clones(repo: str, ref: str = "", min_loc: int = MIN_CLONE_LOC,
           near: float = 0.0, limit: int = 12) -> dict:
    """Structurally identical callables - and optionally near-identical.

    Two tiers, and they are different KINDS of claim:

      identical  same shape_hash. A hash match on the body with the
                 entity's own name stripped, so it is name-insensitive
                 and it is certain. `_cli` and `cli` with the same body
                 are the same code.
      near       sketch_jaccard >= `near`. An ESTIMATE, carrying its
                 score. Off unless asked for, because a similarity
                 threshold invites the reader to treat a number as a
                 verdict.

    With `ref`, answers "what else is this?". Without, answers "where is
    this repo duplicated?".

    Grep cannot do the identical tier at all: the names differ, and the
    bodies differ in whitespace and in the name itself.
    """
    from .query import _ctx
    ctx = _ctx(repo)
    if not ctx:
        return {"error": f"no index at {repo}; run memway init first"}
    _repo, _coord, ix, edges, _meta = ctx

    if near and not (0.0 < near <= 1.0):
        return {"error": f"near must be between 0 and 1, got {near}"}

    callables = [e for e in ix.entities.values()
                 if e.kind in ("function", "method")]
    eligible = [e for e in callables if e.shape_hash and e.loc >= min_loc]
    # NEVER SILENTLY SAMPLED. The floor is a real exclusion and a reader
    # who cannot see its size cannot judge the answer.
    below = sum(1 for e in callables if e.shape_hash and e.loc < min_loc)

    target = None
    if ref:
        target = ix.resolve(ref)
        if not target:
            from .query import _resolve_error
            return _resolve_error(ref, ix, _coord)

    groups = collections.defaultdict(list)
    for e in eligible:
        groups[e.shape_hash].append(e)

    def _row(e, score=None):
        row = {"coordinate": e.coord_id, "qualname": e.qualname,
               "path": e.path, "line": e.lineno, "loc": e.loc}
        if score is not None:
            row["similarity"] = round(score, 3)
        return row

    out: dict = {"min_loc": min_loc, "excluded_below_min_loc": below}

    if target is not None:
        peers = [e for e in groups.get(target.shape_hash, [])
                 if e.coord_id != target.coord_id]
        shown, rep = rank_bound_report(
            [_row(e) for e in peers], "identical",
            rank=lambda r: r["qualname"], cap=limit)
        out.update({"of": target.qualname, "identical": shown, **rep})
        if near:
            from .indexer import sketch_jaccard
            cands = []
            for e in eligible:
                if e.coord_id == target.coord_id or e.shape_hash == target.shape_hash:
                    continue
                s = sketch_jaccard(target.sketch, e.sketch)
                if s >= near:
                    cands.append(_row(e, s))
            shown_n, rep_n = rank_bound_report(
                cands, "near", rank=lambda r: -r["similarity"], cap=limit)
            out.update({"near": shown_n, "near_threshold": near, **rep_n})
        out["note"] = ("identical = same structure hash (name-insensitive, "
                       "certain). near = minhash estimate, a score not a "
                       "verdict")
        return out

    # BOUNDED AT BOTH LEVELS. The first version capped GROUPS at twelve and
    # let each carry every member, which is the same rule applied at one
    # level and forgotten at the level below: pytest's map returned 12
    # groups holding 193 member rows - 36,433 characters, ~9k tokens - and
    # nothing in the payload said a list had been cut, because none had.
    # payload.py's own docstring names this shape ("a list that quietly
    # stops at twelve IS a sampled list"); this one did not stop at all.
    #
    # The COUNT is the actionable part of a clone group - "this body exists
    # 27 times" - and five examples are enough to go look. The rest is
    # reachable by asking about one ref.
    def _group(h, v):
        rows = [_row(e) for e in sorted(v, key=lambda x: x.qualname)]
        shown_m, rep_m = rank_bound_report(rows, "members", cap=MEMBERS_CAP)
        return {"shape": h, "count": len(v), "loc": v[0].loc,
                "origin": _origin(v), "members": shown_m, **rep_m}

    dupes = [_group(h, v) for h, v in groups.items() if len(v) > 1]
    # RANKED BY ORIGIN FIRST, AND NOTHING IS DROPPED. Ranking purely by
    # group size hands the whole page to parametrized tests: 351 of
    # pydantic's 471 groups are test-only, so the twelve shown were
    # test bodies and this never surfaced -
    #
    #   10x 6 lines  _BaseUrl.host / _BaseUrl.fragment /
    #                _BaseMultiHostUrl.query
    #
    # ten identical property accessors in production networking code,
    # which is what somebody runs this to find.
    #
    # A FILTER WOULD HAVE BEEN WRONG. Excluding tests discards real
    # duplication silently, and raising min_loc kills the true positives
    # FIRST - measured: at loc>=5 memway's own _cli and _git groups
    # vanish and 3 of 14 groups survive. So the totals still count every
    # group, the census says how they split, and only the ORDER changes.
    order = {"production": 0, "mixed": 1, "test-only": 2}
    shown, rep = rank_bound_report(
        dupes, "groups",
        rank=lambda g: (order[g["origin"]], -g["count"], -g["loc"]),
        cap=limit)
    census = collections.Counter(g["origin"] for g in dupes)
    out.update({"groups": shown, **rep,
                # Reported as a FACT, not applied as a filter.
                "groups_by_origin": {k: census.get(k, 0) for k in order},
                "duplicated_callables": sum(g["count"] for g in dupes),
                "callables": len(callables),
                "note": ("each group is one body appearing under several "
                         "coordinates; the structure hash ignores the "
                         "entity's own name, so renamed copies still match. "
                         "Production groups rank first - nothing is "
                         "filtered, see groups_by_origin for the split")})
    return out


def covering_tests(repo: str, ref: str, max_depth: int = 4) -> dict:
    """Which tests exercise this coordinate - BEFORE you change it.

    NOT NAMED `tests_for`, WHICH IS WHAT IT IS CALLED EVERYWHERE ELSE.
    pytest collects any module-level callable whose name starts with
    `test`, so `from memway.primitives import tests_for` inside a
    user's test file makes pytest try to RUN it, and it errors with
    "fixture 'ref' not found" - an error that names neither memway
    nor the real cause. Found by importing it into this project's own
    suite, one minute after writing it.

    The CLI command is still `tests-for` and the MCP tool is still
    `memway_tests_for`: neither is imported, so neither can be
    collected, and those are the names a user actually types.

    The same lens verify_change uses after a change (verify.tests_reaching,
    one implementation), asked about an entity you have not touched. The
    tiers carry their own epistemics:

      grounded  reached through resolved graph edges. Evidence.
      name_hit  a test file whose text mentions the short name with no
                edge into the impact set. A GUESS, and grep can produce
                exactly this tier and no more.

    The pairing is the point. "12 grounded, 3 by name" tells you what is
    provably covered and what merely looks covered; a single merged number
    would tell you neither.
    """
    from .query import _ctx, _resolve_error
    from .verify import tests_reaching
    ctx = _ctx(repo)
    if not ctx:
        return {"error": f"no index at {repo}; run memway init first"}
    repo_p, coord, ix, edges, _meta = ctx
    e = ix.resolve(ref)
    if not e:
        return _resolve_error(ref, ix, coord)

    grounded, name_hit, other, reached = tests_reaching(
        ix.entities, edges, [e.coord_id], repo_p, max_depth)

    g_shown, g_rep = rank_bound_report(sorted(grounded), "grounded")
    n_shown, n_rep = rank_bound_report(sorted(name_hit), "name_hit")
    return {
        "of": e.qualname,
        "coordinate": e.coord_id,
        "grounded": g_shown, **g_rep,
        "name_hit": n_shown, **n_rep,
        "other_language": sorted(other, key=lambda t: t["test"]),
        "reached": len(reached),
        "note": ("grounded tests are reached through resolved edges; "
                 "name_hit files merely mention the name and are a guess - "
                 "the tier grep would give you. reached counts every "
                 "coordinate walked, not just tests"),
    }
