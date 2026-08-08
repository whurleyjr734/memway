# Design 001 — Knowledge Prune / Decay
Status: designed, gated on one month of real note accumulation.

## Problem
Agent write-back (coordsys_meta) will accumulate notes. Unmanaged, the
store degrades two ways: redundancy (agents re-observing the same fact
across sessions) and rot (notes that stop being true). Which failure
arrives first is unknown — instrument before building.

## Principles
- Demote, never silently delete (a wrongly-pruned note is the same
  poison as a wrongly-migrated one).
- Lifecycle tiers: active -> stale (exists today, automatic via
  logic-hash) -> archived (hidden by default, retrievable) ->
  compacted (only under review or multi-signal agreement).

## Decay signals (compose, don't pick one)
1. Staleness duration: stale across K re-indexes and entity edits.
2. Supersession: minhash the note TEXT (sketch machinery works on
   prose); newer entry on the same coordinate semantically covers older.
3. Confirmation: a re-stamp against current logic hash resets the clock
   (cheap channel="confirm" write; promotion counterweight).
4. Author-class trust: agent notes decay faster than human notes unless
   confirmed.
Weight usage-frequency LOW: notes on rarely-touched code are most
valuable exactly when that code is finally touched.

## Prevention beats pruning
- Write-time dedup in coordsys_meta: sketch-similarity against existing
  entries -> merge or "similar note exists, confirm instead?"
- Per-coordinate display budget in briefings: top-K by
  confirmed/trust/recency, "+N archived" behind a flag.

## Compaction
Agents digest N aged entries into one author="auto-digest" summary
citing sources; originals archived, never destroyed. Append-only truth,
compressed view — the lineage philosophy applied to prose.
