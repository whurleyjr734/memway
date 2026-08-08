# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
