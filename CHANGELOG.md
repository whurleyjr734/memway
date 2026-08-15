# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.50.0] - 2026-08-15

### Added

- **`memway dig <repo> <ref>`** and **`memway_dig`** (MCP tool #10) — mine
  ONE entity's history on demand. Resolves through the map, walks
  `git log -L` over its exact line range, labels commits that predate the
  entity, follows `(#NNNN)` references to forge PR bodies, and reports
  which releases each commit shipped in. **Returns candidates only**: it
  never gates, never scores, never writes. Judging rationale vs
  restatement is the caller's job.

- **`memway viz <repo>`** — the real map as a single self-contained
  interactive HTML file. Stdlib only; D3 stays a CDN reference. Refuses
  above 1500 entities without `--filter <prefix>` or `--force`, and never
  samples silently. Knowledge is read through `MetaStore`, so staleness
  and channel labels render truthfully rather than being re-derived.

- **`memway console <repo>`** — the explorer served live, with the read
  tools as buttons on each coordinate card. Binds **127.0.0.1 only** with
  a random per-session token on every request; `probe`, `index` and
  `attention` have no endpoint at all (a browser button that executes
  repository code is a different trust model). The single write is a note
  at a coordinate, receipted.

- **The evidence layer.** Excavated material now splits in two:
  *evidence* (what a commit or PR SAYS — derived, stored at
  `.coord/evidence/`, gitignored, clearable) and *verdicts* (what a reader
  CONCLUDED — authored, in `.coord/meta`, of the form
  `VERDICT <ref>: <judgment>`). Bodies are stored exactly once; a verdict
  points at evidence rather than restating it, and still renders — marked
  — if the evidence is cleared. `dig --cache` populates it; a cache hit
  walks no history.

### Fixed

- **Read tools no longer warm the pickle caches.** `viz` and `dig` pass
  `write_cache=False` through `load_json_cached` / `Indexer.load_existing`
  / `EdgeBuilder.load`. A tool that promises it does not touch `.coord`
  cannot warm a cache as a side effect — it breaks a read-only checkout
  and makes "did anything change?" unanswerable. **Partial: the
  `query.*` reads still warm both caches unless wrapped in
  `query.read_only()`, which today only the console's GET endpoints do.**
  See the release notes below.

### Changed

- **A taxonomy, recorded because getting it wrong broke a feature.**
  "Derived" is two categories, and only one is safe to skip on a read:
  *regenerable-from-source* (`cache/`, `evidence/`) can be recomputed at
  will; *snapshot-baseline* (`docbindings.json`, `versions/`) IS the
  reference an answer is measured against. Suppressing the docbindings
  write unconditionally made every design-doc binding read permanently
  "fresh" — drift detection silently died, and two existing tests caught
  it.

### Known limitations

- The read fence covers `viz`, `dig` and the console's HTTP surface.
  Direct `query.before_edit` / `show` / `summary` / `at` / `lineage` calls
  — the CLI and MCP paths — still warm `.coord/cache/*.pkl`. Not a
  regression (they always did), but not yet the whole fix.
- `feature/excavate` is **not** in this release. It carries
  `PARSE_SCHEMA_VERSION 4` and a documented one-time comment-rot
  re-baseline for Go/TS/JS/Java maps; it is sequenced separately with its
  own migration note.

## [0.49.2] - 2026-08-08

### Fixed

- **Design-doc scanner no longer binds `docs/**/examples/**`.** Publishing
  an examples folder from `/docs` bound files that govern no coordinates,
  which rewrote `.coord/docbindings.json` on every reindex and left a dirty
  map in a clean tree.

### Added

- **Contribution terms.** `CONTRIBUTING.md` and `CLA.md` — the project uses
  a Contributor License Agreement so it can keep future licensing options
  open, including commercial licenses. Contributors keep copyright in their
  own work.
- **Optional capture hook example** under `docs/examples/hooks/`. A `Stop`
  hook that refuses to end a session that recorded nothing. Not installed
  by `memway setup`; opt in deliberately or not at all.

### Changed

- **The capture rule fires on refusals and investigations, not just edits.**
  Reasons that surface while declining, blocking on, or investigating a task
  are now explicitly in scope — a constraint strong enough to stop work is
  exactly what the next session needs, and it was the case most often lost.

## [0.49.1] - 2026-08-08

Initial public release.

[Unreleased]: https://github.com/whurleyjr734/memway/compare/v0.49.2...HEAD
[0.49.2]: https://github.com/whurleyjr734/memway/releases/tag/v0.49.2
[0.49.1]: https://github.com/whurleyjr734/memway/releases/tag/v0.49.1
