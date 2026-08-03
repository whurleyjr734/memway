# Design 003 — Observed Type Profiles
Status: designed; first post-launch build. Successor to the REVERTED
forward-ref unwrap (post-mortem in parsers.py) — this design only ever
claims types it witnessed.

## Idea
The coordinate as a type-knowledge accumulator. Three provenance tiers:
- declared: annotations (structured param_types/return_type shipped v49.1)
- observed: the probe holds every argument at every call; record
  type(v).__name__ per param, aggregated with counts
- noted: agent observations via coordsys_meta
All staleness-stamped against logic_hash; all confessed by source.

## Payoffs (each solves a measured problem)
1. Safe receiver resolution for untyped/forward-ref code: observed
   receiver types qualify edges at resolution="observed" — witnessed,
   not guessed (the guard the reverted experiment lacked).
2. Divergence alarms: declared str, observed bytes -> wrong annotation
   or live bug; the coordinate is the only place both facts coexist.
3. Briefings on legacy code: "(Request, dict) -> Response, 47 samples."

## Extensions (value order)
value shapes (observed dict key-sets) > exception profiles > de facto
optionality > contract synthesis (annotation-diff PRs from profiles) >
type drift across lineage > real-input fixtures (secrets flag required).
Resist: duck-typing via attribute tracing (proxy-wrapping; invasive).

## Referee plan
Unit tests; real-repo A/B on flask+rich resolution histograms (the
reverted experiment's own test); probe-driven demo before any claim.
