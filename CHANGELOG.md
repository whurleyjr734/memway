# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.53.1] - 2026-08-16

### Fixed

- **The origin toggle and test styling were inert in emitted maps.** The
  test/source lens shipped in 0.52.1 with a correct payload, correct markup
  and a correct predicate, and did nothing: the template's `normalize()`
  rebuilds every node field by field, and `is_test` was not on the list. So
  every node reached the renderer as `undefined`, the predicate classified
  all of them as source, unchecking "tests" hid nothing, unchecking "source"
  hid the entire graph, and the hollow-core styling never applied. Reported
  from a live console session on itsdangerous. One field added; measured on
  that repo, the filter now partitions 169 entities into 96 source and 73
  tests.

### Changed

- **The test class for interactive behaviour is upgraded from presence to
  execution.** Every assertion guarding the lens was of the form
  `"..." in html`, and all of them stayed green while the feature was dead.
  The suite now runs the template's own `normalize()` and filter predicate,
  lifted verbatim, in node when node is available, plus a Python replica
  that parses `normalize()`'s real field list so the guarantee survives on a
  machine with no JS runtime.

  Tests named for a runtime behaviour they cannot observe have been renamed
  to say what they actually check, including one previously called
  `test_viz_origin_toggle_is_wired_not_merely_present`, whose name described
  the exact thing it failed to do. Presence remains as a fast smoke layer;
  it is no longer the only witness for anything interactive.

## [0.53.0] - 2026-08-16

### Added

- **`memway index --if-stale`** reindexes only when the tree has moved past
  what the map describes, comparing a newly recorded `indexed_at_sha` in the
  manifest against `git rev-parse HEAD` plus a dirty check. The current path
  is a read: measured at 50-100ms and asserted byte-identical against
  `.coord`, so it belongs under the read fence. Outside a git repo, or with
  no map, it says so and exits 0 — this runs from hooks, and a freshness
  check must never be the reason a commit fails.

  The dirty check excludes `.coord` itself. memway tells you to commit the
  map, so an index modifies a tracked path by definition; counting that made
  `--if-stale` see a dirty tree immediately after a successful index and
  reindex forever. Caught on a fixture, and now a test.

- **`memway hooks install` / `uninstall`** writes `post-commit`,
  `post-checkout` and `post-merge` hooks that run
  `memway index . --if-stale --quiet`. Opt-in only: `setup` advertises it in
  one line and never installs it, because a tool that writes into
  `.git/hooks` uninvited has taken something it was not offered.

  Existing hooks get the memway line inside a marked block, the same
  discipline the rules files use, and `uninstall` removes exactly that block
  leaving the rest byte-identical. A hook whose control flow cannot be read
  safely is refused with instructions rather than modified. **A block is
  never appended after an `exit`**: git's own sample hooks end in `exit 0`,
  and appending past one installs cleanly, reports success, and never runs.

  A hook failure never blocks the git operation. Shell-level failures are
  swallowed by `|| true`; an exception inside memway is logged to
  `.coord/log/hooks.log` and reported in one line. Both paths tested.

- **Every read tool reports a lagging map.** `summary`, `show` and
  `before_edit` gain a `map_lag` key, `before_edit` also appends it to
  `warnings`, and the CLI prints one note line:

      note: map indexed at 652f58d, HEAD is f2e6bc3 (7 commits ahead) - run memway index

  **This is the actual guarantee, not the hooks.** Hooks cannot fire during
  a bisect, in a fresh worktree, on a hand-edited tree, or anywhere nobody
  ran `hooks install`. The map may lag; it must never lag silently. A map
  with no recorded sha reports nothing rather than guessing, so maps written
  by older versions are not called stale.

  Written because memway's own map sat seven commits behind across three
  commits that changed parsing, hashing and entity extraction, with the
  re-index rule written down and followed by nobody. The symptom presented
  as stale comment-rot, because a reader cannot tell "your comment drifted"
  from "your map is old".

### Fixed

- **The corrected language-support prose reaches PyPI.** The README is the
  project description, so the claims fixed in this cycle - method signatures
  extracted for Go, JavaScript and TypeScript but not Java; leading doc
  comments lifted for Go and JavaScript; no field declarations as entities -
  were right on GitHub and memway.io while PyPI still served the old, false
  wording frozen into 0.51.1's metadata. A release is the only way to move
  it, and this one carries it.

## [0.52.1] - 2026-08-15

### Added

- **`memway --json verify-change <repo>` is the seventh query.** It answers
  "given the working tree against the saved map, what changed and what
  guards it": changed entities, the impacted radius, and the tests that
  reach the change through the edge graph, tiered into `grounded` (a real
  edge got there) and `name_hit` (a labelled guess).

  It **reports, it does not run**. `run` is pinned False and takes no
  argument on this surface: selecting tests is a read, executing them is
  not, and a query that shells out to pytest is a different tool. Same
  function the MCP tool calls, never a second implementation, asserted.

  **It is the one query that writes.** Every other entry in `QUERIES`
  leaves `.coord` byte-identical; this one re-indexes and rewrites the edge
  cache so the map reflects the tree it just measured. That is the MCP
  tool's long-standing behaviour, shared deliberately rather than forked,
  and a test records it so nobody infers inertness from the company it
  keeps.

  With the CLI equivalent existing, rule 2 of the emitted rules files drops
  its "MCP only" apology and names the command. All three files regenerate
  byte-identical.

- **The `--json` usage text lists every query.** It had silently omitted
  `dig` since 0.50.0, and is now pinned to `QUERIES` by a test.

### Changed

- **Aggregate views distinguish tests from source. Presentation only.** No
  metric, edge, or stored byte changes; the same numbers are partitioned two
  ways for reading, and a test asserts `.coord` is byte-identical across a
  summary call.

  `verify.is_test_entity` is now the one rule, shared by the summary and the
  map, and it reads PATH AND FILENAME only, never the qualname: a function
  called `test_connection` in production code is production code. It also
  matches `foo_test.go` and `foo.spec.ts`, which sit beside source rather
  than under `tests/`, so a path-prefix rule alone would misclassify every
  Go and TypeScript repo.

  `--json summary` gains `hardest_overall` (top-N across everything, each
  entry flagged) and `entities_by_origin`; every `hardest` entry gains
  `is_test`. Existing keys are untouched and `hardest` keeps its exact
  meaning, source only, because consumers already depend on it.

  `viz` and `console` gain a source/tests toggle in the filter rail, both on
  by default, using the same machinery as the kind filters. Test entities
  render with a drained fill rather than a colour of their own, which would
  imply a sixth kind they are not.

- **Fixed a latent bug while doing it.** The hardest list excluded tests with
  `"test" not in e.path.lower()`, a substring match that would have silently
  dropped any source file whose path merely contains those letters
  (`memway/latest.py`, `contest/`, `protest/`). No file in this repo hits it
  today, so the swap changes nothing here and closes it before it bites.

## [0.52.0] - 2026-08-15

### Changed

- **The workflow rules are emitted to `AGENTS.md`, `CLAUDE.md`, and
  `GEMINI.md`** instead of `CLAUDE.md` alone. `AGENTS.md` is canonical; the
  other two are byte-identical copies written from one template in the same
  pass. A client reads the filename it knows and ignores the rest, so a repo
  carrying only `CLAUDE.md` gave every non-Claude agent no rules at all: it
  then worked correctly and recorded nothing, which is the exact failure the
  rules exist to prevent and is invisible while it happens. A test asserts
  the managed blocks are byte-identical across all three, so drift between
  them is a failing test rather than something noticed later.

- **The rules are phrased tool-name-neutrally.** Exact MCP names, with the
  CLI equivalent named in parentheses where one exists, so a client with no
  MCP server can still act on them. `memway_verify_change` has no CLI
  equivalent today and the rules say so rather than inventing a command.

- **Existing rules files are upgraded, or refused.** The emitted block is
  delimited by an HTML comment marker; anything below the end marker is
  yours and survives every later `setup`. A file carrying an older memway
  block, unedited, is upgraded whole. A file that memway cannot prove it
  wrote is **left alone and reported**, with the marker to add if you want
  it managed. Refusing one filename does not refuse the others.

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

### Documentation

- **README truth-synced.** It had drifted 18 commits: it advertised nine MCP
  tools (there are ten), 133 tests (369), claimed `viz` output was
  self-contained while the template linked a CDN, quoted a stale `CLAUDE.md`,
  and promised a dogfooding ledger in `docs/` that is not there. It also
  predated `dig`, `evidence`, `viz`, `console`, and the registry entirely.
  Now carries the pull-a-map funnel, the fourteen commands, a registry
  section, and honest limits refreshed against 0.51.1 — including two that
  were missing: the polyglot parsers are thinner than Python's, and knowledge
  is coordinate-scoped, so lessons about *how you work* have no home in the
  map today.

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
