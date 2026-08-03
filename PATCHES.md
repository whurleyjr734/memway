# coordsys v48 -> v49 patches (benchmarked session)

## Fix 1 — indexer.py: AST-based shape_hash
Old: hash(body_text.replace(short_name, "")) — missed 20% of pure renames
when the old name lingered in a docstring/comment/recursive call.
New: hash of AST node-type sequence (name-insensitive by construction),
fallback to name-stripping for non-Python. Rename recall: 0.80 -> 1.00.

## Fix 2 — indexer.py: duplicate-qualname salting in _assign
Old: @overload stubs / try-except defs / TYPE_CHECKING branches silently
overwrote each other (5 defs of _encode_params -> 1 coordinate, metadata
merged). New: occurrence-indexed qualnames (name, name#2, ...), file-order
deterministic so occurrence identities persist across re-indexing.

## Fix 3 — indexer.py + lineage.py: sketches, scored matching, tiers
- Entity gains a 48-perm minhash sketch + shingle count (~100B/entity):
  similarity is now computable from the index alone (old source text is
  gone by the time detect_lineage runs — this was the architectural
  reason the old passes were exact-match only).
- detect_lineage: body-hash pass kept, then scored greedy matching
  (name/sketch-Jaccard/AST-shape/signature/loc) with margin + corroboration.
  Confident -> author="auto" + metadata migration (shape-identical matches
  re-stamp new_body_hash so migrated notes are not flagged stale).
  Uncertain -> author="pending-review", NO migration until confirmed.
- Split/merge detection via sketch containment; one-to-many evidence
  outranks one-to-one (split suspects are never consumed by pair matching).
- Order: exact hash -> auto pairs -> splits/merges -> pending pairs.

## Benchmark (172 real-code refactor links, psf/requests, 30 trials)
v48 stock:    0.500 recall, silent misses, 0 false links
v49 patched:  0.702 auto, 1.000 with review, 0 silent misses, 0 false links
Test suite: 97 passed / 14 failed — identical to pristine v48 baseline
(all 14 failures pre-existing, unrelated to these patches).

## v49.1 — post-live-session additions
- Grounding block in before_edit (edge provenance + low-confidence count).
- coordsys_meta: MCP tool #8, agent write-back with body-hash stamping.
- __main__.py shim; sketch_jaccard normalizes by compared length.
- Inheritance: structural `inherits` edges from the hierarchy pass;
  before_edit gains bases/subclasses/overrides/dispatch-shape and an
  OVERRIDDEN warning; knowledge flows down the MRO with provenance
  (inherited_from, hops_up) and ancestor-hash staleness.
- logic_hash: behavior-sensitive, cosmetics-insensitive tier between
  body_hash and shape_hash. Staleness, metrics memoization, and a new
  certain-tier lineage pass all key on it: comment/docstring edits no
  longer cry wolf or bust caches; renames-with-doc-tweaks auto-confirm.
  Readers pass an accepted-hash set {logic, body} for transition safety.
- Structured types on coordinates: param_types/return_type harvested
  from annotations onto every entity; edges gains a guarded 'annotated'
  tier (0.90, fires only when the declared type resolves to a real
  in-repo class). Parser-side blind unwrap of Optional/union/string
  annotations was tried and REVERTED: A/B on flask showed it
  manufacturing 12 guess-tier edges. Receiver-preserving resolution
  through the guarded tier is the documented follow-up.
- Comment channel: comments harvested per-entity (tokenize) with
  TODO/FIXME/HACK markers surfaced in briefings; COMMENT ROT detection
  (comments unchanged across a logic_hash change -> flagged). Design-doc
  binding (docs/design/004, now SHIPPED): backticked entity refs in
  docs/**/*.md bound to coordinates; logic drift past the doc's last
  touch -> advisory in briefings. Wild-tested on flask.
