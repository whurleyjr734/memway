# Design 002 — Knowledge Refactoring at Edit Time
Status: designed, gated with 001 (same month of accumulation).

## Problem
When code changes, attached knowledge can be mechanically wrong (stale
identifier names) or semantically wrong (describes old behavior). Naive
auto-rewriting launders staleness into false freshness — worst exactly
where notes matter most (contracts), because a contract note's staleness
may mean THE CONTRACT WAS JUST VIOLATED. The divergence is information.

## Tiers by rewriteability
- AUTO (mechanical, provably safe): identifier renames mentioned in note
  text (lineage knows the rename), signature refs, dead line numbers.
  Silent, like shape-identical re-stamping today.
- DRAFT (semantic, never in place): at edit time — when the diff, old
  note, and intent coexist — emit author="agent-draft" with a
  `supersedes` link (knowledge getting its own lineage records).
  Briefings show "stale note + proposed update, unconfirmed."
  Confirmation promotes draft, archives original.
- NEVER (historical/intentional): incident references, threshold
  rationale, design intent. Staleness escalates instead of resolving:
  "the code this contract described changed — verify the contract
  holds" is the single most valuable alert the system can produce.

## Requirement
Note-kind field at write time: descriptive | contract | historical
(default descriptive). Misclassification risk documented: a contract
note auto-drafted over is the poison scenario — hence never-replace-
silently is load-bearing, not polish.

## Addendum (shipped early): comment-draft approval loop
The DRAFT tier for comments requires no new machinery — it composes
from shipped parts plus the client's native edit-approval UI:
1. rot detection flags the divergence (shipped)
2. the warning/attention text instructs the agent to DRAFT updated
   comments and propose the edit (shipped: workflow nudge in payloads)
3. the MCP client's edit-approval flow IS the approve/reject prompt —
   the pre-generated comment sits in a pending diff awaiting one click
4. an applied edit changes comment_hash -> rot clears on re-index;
   a rejected edit leaves the flag standing (demote-by-acknowledgment)
The server stays LLM-free: the client agent is the generator, the
human is the gate, the map is the detector and the ledger.
