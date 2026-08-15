# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.51.1] - 2026-08-15

### Fixed

- **`viz`/`console` output is now airgap-safe — no external requests.** The
  emitted page linked d3 from cdnjs and two stylesheets from Google Fonts, so
  a rendered map required network access and announced itself to two third
  parties on open. d3 7.8.5 is now vendored (`memway/vendor/`, ISC, notice
  shipped) and inlined at render time; the webfonts are replaced by system
  stacks. The emitted file grows from ~442KB to ~606KB — the correct price.

  Both surfaces are covered because they share one template reader
  (`viz.load_template`), and `tests/test_airgap.py` asserts the property on
  the **emitted bytes** of each: no `src=`/`href=`/`url()`/`@import`/
  `@font-face`/resource hints to an absolute URL, no `fetch`/`XMLHttpRequest`/
  `WebSocket`/`iframe`, and every remaining absolute URL matched against a
  pinned allowlist of inert constants (d3's ISC attribution banner and five
  W3C namespace identifiers, which `createElementNS` needs and nothing ever
  fetches).

  Found by an acceptance sweep, not by the suite — because two existing tests
  *enforced* the CDN link and the webfont names. The guard was not missing, it
  was inverted. Both now assert the opposite.

- **Registry failures stop swallowing context.** A mistyped map name produced
  `pull failed: HTTPError: HTTP Error 404: Not Found` — no name, no URL, and
  no hint that an index of real maps exists, so a typo was indistinguishable
  from an outage. Every fetch failure now names what was being fetched and
  from where; a 404 on the bundle points at the releases page, because a 404
  is overwhelmingly a guessed name. A 404 on the `.sha256` is reported as its
  own failure — the map exists but cannot be verified — rather than as a
  missing map. Network faults keep their exception class and reason, and an
  already-contextual error is never re-wrapped in a generic one.

- **`--version` is correct under editable installs.** `importlib.metadata`
  describes the *install event*, and for `pip install -e` that froze at
  wire-up time: this repo's own dev venv reported `memway 0.49.2` for weeks
  while running 0.50.1 source. Metadata still wins for a wheel; for an
  editable install the source tree *is* the install, so `__version__` wins.
  Two checks, because either alone is insufficient — `direct_url.json`
  answers "was this `-e`", and a location test covers the case where a
  leftover `memway.egg-info` is what `importlib.metadata` resolves at the
  repo root, since that carries no `direct_url.json` at all. The answer no
  longer depends on the current directory.

## [0.51.0] - 2026-08-15

### Added

- **`memway pull <name>[@version]`** — fetch a published map and install it
  into `.coord/`. A map is worth more when you do not have to build it:
  someone indexes a large dependency once and everyone else inherits the
  coordinates and the knowledge attached to them.

  A bundle is a tarball from the network and is treated as hostile until
  proven otherwise: the SHA-256 checksum must match before anything is
  unpacked, every member is validated *before* extraction, members must
  resolve inside the target and live under `.coord/`, and links and devices
  are refused outright. Modes are normalized rather than trusted, because
  the stdlib's safe-extraction filter only exists on Python 3.12+ and this
  package supports 3.10.

  `--force` replaces the derived index and *merges* the bundle's knowledge
  into yours; locally authored entries are never deleted. `--replace-meta`
  is the destructive path and deliberately does not imply `--force` — you
  have to type both, and typing both is the moment you notice which you
  asked for.

- **The registry is live.** `itsdangerous`, `flask`, and `httpx` are
  published at
  [whurleyjr734/memway-maps](https://github.com/whurleyjr734/memway-maps),
  each pinned to the upstream commit it was indexed from.

### Changed

- **Manifest v1.** A bundle's `.coord/manifest.json` carries `name`,
  `upstream_repo`, `upstream_sha`, `memway_version`, `license`, and
  `built_at`. `registry._describe()` is the single reader — it normalizes
  the older `repo`/`sha` aliases into the v1 names, so the already-published
  bundles keep installing and nothing downstream of that function knows two
  schemas ever existed. Drift is measured against `upstream_sha`.

### Notes

- **No MCP tool for `pull`, deliberately.** It fetches over the network and
  writes a directory tree to disk; that pair stays behind a human typing a
  command rather than behind a model deciding to call it. A test asserts no
  registered tool name contains `pull`.

## [0.50.1] - 2026-08-15

### Fixed

- **`memway --version` (and `-V`) now works.** It was not a command, so
  it fell through to the usage path and exited 1 — the first thing
  anyone types after installing, and it looked broken. Handled before
  dispatch: prints `memway <version>`, exit 0. The version comes from
  `importlib.metadata.version("memway")`, which reflects what was
  actually installed, falling back to the package `__version__` for
  source-tree and editable runs. A test pins `__version__` to
  `pyproject.toml` so the two cannot drift.

- **`dig` now says when a clone is shallow.** Every count it prints is a
  lower bound on a `--depth N` clone, and "1 commit touched this range"
  reads as a fact about the code when it is a fact about the clone. A
  shallow repo now adds one line after the count, and a `warnings[]`
  entry in the MCP payload:

      note: shallow clone - history truncated; counts are a lower bound
      (git fetch --unshallow for full history)

  Detected via `git rev-parse --is-shallow-repository`, falling back to
  the `.git/shallow` marker for git versions that lack the flag. Output
  on full clones is byte-identical to 0.50.0, with a regression test.

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
  **The fence is 7/7**: `before_edit`, `show`, `summary`, `at`, `lineage`,
  `viz` and `dig` all leave `.coord` byte-identical. `query._ctx` no
  longer warms either cache at all - `memway index` writes them, reads
  consume them - and `docbindings.json` is written only when its content
  actually changes, so the snapshot baseline survives while a repeat
  briefing is a genuine no-op.

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
