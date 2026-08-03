# Design 004 — Design Documents as a Knowledge Channel
Status: designed this session; Tier 2 (build on demand).

## Idea
ADRs/design docs are repo-level "why" the way comments are line-level
"why" — and they rot worse, because nothing ties them to code. Bind
them to coordinates:
- Harvest docs/design/*.md (harvest.py already mines docstrings + git);
  resolve entity references in doc text to coordinates.
- Briefings on governed entities surface "governed by 003 §2."
- When governed entities' logic hashes drift past the doc's last-touch,
  flag the DOC: architecture-decision rot detection. No ADR tooling
  does this, because none has identity underneath.

## Notes
Reference syntax: explicit (backticked qualnames) resolves cleanly;
fuzzy prose matching stays out of v1 (manufactured-edge lesson, see
parsers.py post-mortem). Doc staleness is advisory — a WARNING channel,
never a block.
