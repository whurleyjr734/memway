# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.56.2] - 2026-08-17

Theme: **the shop window is derived too.**

`docs/map.html` had drifted **nine releases**. Last rendered at `abc5cf7`,
it was still being served while v0.54.3 through v0.56.1 shipped. On
`memway.parsers` the live site showed:

```
memway/parsers.py:1-731    complexity 143
confirm: "PARSE_SCHEMA_VERSION is 6, not 5"   <- badged FRESH
```

All three wrong. The constant had been 7 since 0.56.0, that entry was
stale **and** superseded in the committed map, and the confirm saying so
was absent from the page entirely — a superseded note presented as
current, with a freshness badge earned against a hash that had moved.
Found by a human reading the page, not by any check.

Same root as lesson 10: `.coord` rides every commit through the
pre-commit hook, and this rendered artifact rode nothing.

### Added

- **`pytest -m release`** — a new marker for gates comparing a DERIVED
  artifact against the committed map. Deselected by default, because
  `.coord` moves on nearly every commit and running these per-save would
  keep the suite red for ordinary work and train everyone to ignore it.
- Three gates: the page's entity/edge counts must match the committed
  map; no rendered entry may lack a `superseded` flag or claim two
  unsuperseded entries in one channel; and the render summary must ask
  the ring rule.
- CLAUDE.md's close ceremony regenerates `docs/map.html` and runs
  `pytest -m release`, with the reason recorded beside it.

### Fixed

- **The render summary counted raw stale entries.** It announced
  "81 stale" on a repo whose decisive queue was **zero** — 81 being
  superseded history somebody had already answered. The rings were right
  the whole time (`has_unsuperseded_stale`); only the number a human
  reads was raw. Fourth appearance of this exact shape after `attention`
  (0.55.2), the queue truncation (0.55.4) and module rot (0.56.1).
- `viz.py` spelled `from .metadata import ...` in **four** separate
  function bodies, which is how the summary came to miss a rule that was
  three lines above it. Collapsed to one module-level import, as
  `query.py` was in 0.55.2.
- The ring-rule pin matched an exact import *string* and broke on the
  refactor that strengthened what it guards. It now checks the property
  via AST. A pin that fails on correct code gets deleted, not obeyed.

## [0.56.1] - 2026-08-17

Theme: **a signal must have a boundary.**

**Module docstrings describe behavior no hash can bound — files,
functions, and other modules — so memway no longer flags them as rot.
Module docstring review is a deliberate human task; function/method/class
rot is unchanged and precise. The 14 standing module flags are cleared as
category errors, not as fixed.**

### Why

Comment rot asks *"did the comments stay while the logic moved."* For a
function that is exact: its comments and its body share one scope. Applied
to a module it never could be. This repo's own `indexer.py` docstring
claims things about its module surface, about behaviour inside its
functions, **and about `lineage.py`** — three scopes in one paragraph.

Hashing the whole file (through 0.56.0) re-flagged every module on every
edit, and since a module's logic hash aggregates its contents a confirm
could never stick: **14 permanent coral entries** that no amount of
reading could clear. 0.56.0 itself created two of them by editing
`probe.py` and `verify.py` for two lines each.

Hashing only the module's *surface* was the planned 0.56.1 and is worse
where it counts — clearable, but silently blind to a docstring describing
behaviour a function body implements. Confident and incomplete is the
failure mode this project treats as most serious. So the check ends
rather than getting a better approximation.

### Changed

- `comment_rot` is not computed for module entities. **At the
  computation**, in `indexer._assign` — not a display filter, so no dead
  flags sit in `.coord` for the next reader to trip on.
- `attention`, `verify_change` and the census therefore read a true zero
  for modules; `comment_rot_total` counts only boundable entities.
- The module filter added to `rotted_comments` in 0.55.4 is now
  redundant. It stays as a second guard and **says so**, rather than
  reading like a live rule.
- CLAUDE.md gains lesson 12: before adding a check, ask what bounds the
  thing it claims to check; if nothing does, the honest build is no check
  rather than an approximate one wearing a precise name.

### Not shipped

`surface_hash` dies with the scope. The `Entity(**e)` filter from 0.56.0
keeps that door open, and nothing needs to go through it now — an
honestly-named `module_surface_changed` prompt can earn its way in later
on a witness's ask.

## [0.56.0] - 2026-08-17

Theme: **one name, one producer.**

A qualname can carry a disambiguator — Java overloads register as
`Type.method/2`, same-named Python defs as `name#3` — and those two
suffixes were produced in two different layers by two pieces of code that
had never heard of each other:

```
parsers.py:670   q = f"{owner}.{name}/{a}"        the Java PARSER
indexer.py:747   qualname = f"{qualname}#{occ}"   the REGISTRY
```

Meanwhile every call target was emitted **bare**. The registry stored one
spelling and the emitter wrote another, and resolution compared the wrong
ends. Neither suffix was wrong; both disambiguate real ambiguity. The
defect was that only one side of the conversation knew the convention.

`refs.py` is now the only place a reference is spelled — `render`,
`base_of`, `short_of`, `arity_of` — called by the parsers, the registry,
the resolver, the verifier and probe alike.

### Fixed

- **Java call edges reach methods.** Measured on google/gson at the same
  clone: resolved call edges **1,770 → 4,919**, of which **3,150 are
  `method → method` where there were zero**. `Gson.newJsonWriter/1` reads
  **callers 7, blast 34** where it read `0` and `0`. The remaining 1,769
  `method → class` are constructor calls, which correctly target types.
  Arity comes from the **argument count at the call site**, so the
  overload actually called is the one that resolves — and the edge is
  `resolution: exact`, not a bare-name guess.
- **`@overload` stubs no longer steal the call.** Python occurrence
  numbering happens **once, in the parser**, read by both the entity walk
  and the call attribution. `Response.iter_content#3 → generate` resolves;
  previously the call was credited to the first stub and the real
  implementation showed no edge.
- **A bare name reaches a disambiguated registration.**
  `memway show gson upperCaseFirstLetter` answers instead of claiming no
  entity matches.
- **`Entity(**e)` ignores fields it does not know** — the last unguarded
  door of the `via_attr` class. `load_raw_edges` learned this in 0.54.3
  after a long-running MCP server died on
  `unexpected keyword argument 'via_attr'`; its sibling never did.
  Verified before fixing: `Entity(**e)` with an unknown key raised.
  **It ships a release BEFORE the field it exists to permit** (`surface_hash`,
  0.56.1), because the filter lives in the reader and only helps builds
  that already have it.

### Corpus floors, re-measured

| repo @ sha | recall | floor | closure misses | ceiling |
|---|---|---|---|---|
| psf/requests @ 8068356 | **98%** | 96% | **0** | 1 |
| pallets/click @ cbd7a41 | **70%** | 68% | **0** | 1 |
| Textualize/rich @ 9d8f9a3 | **98%** | 96% | **0** | 1 |

The oracle itself had to be normalised through `refs`: keyed on the raw
last segment it read `iter_content#3` where the ast oracle said
`iter_content`, which moved requests from 98% to 94% and looked exactly
like a regression.

### Schema

**`PARSE_SCHEMA_VERSION` 6 → 7.** Verified, not reasoned: the cache
stores raw edges *and* entities, so patching the emitter with a warm
cache yielded the OLD `dst_ref` and the new one only after clearing it.
Existing maps re-parse once on first index under this release.

### Known limit, asserted rather than assumed

The unresolved-reference counter added in 0.55.5 **cannot see a
disambiguator `refs` does not know** — recognising an unfamiliar suffix
requires already knowing it is one. The guarantee against that class is
`refs` being the sole producer, not the counter noticing afterwards. A
test asserts the limit so it cannot be sold as a general desync detector.

## [0.55.5] - 2026-08-17

Theme: **stop being wrong quietly.** Four fixes, each closing a case
where memway stated something it could not support.

### Fixed

- **A blind resolver now says it is blind.** `before_edit` reported
  `callers 0, blast 0` with `is_lower_bound: FALSE` for every Java
  method — an affirmative claim that the zero was complete, when 0 of
  1,770 resolved call edges in google/gson reach a method at all. The
  radius is now a lower bound whenever an emitted call reference names
  this entity and the resolver found **no candidate to judge**. This does
  not fix Java resolution; it stops the report lying about it.
  Deliberately narrow: a reference the guards *refused* is a decision,
  not a gap, and counting refusals made the warning fire on healthy
  Python — the 43-vs-3 failure in a new place.
- **The lower-bound warning named the wrong cause.** It said "dynamic
  event emission reached" unconditionally, because that was the only
  cause until now. It derives its reason.
- **The longest-standing flake is closed** — ~3 sightings in ~40 runs,
  never reproducible in isolation. `test_package_version_matches_pyproject`
  asserted against the **imported** module. Python's timestamp `.pyc`
  invalidation checks only source mtime and size at one-second
  granularity, and a version bump changes **neither**: `"0.55.3"` and
  `"0.55.4"` are the same length, and the edit lands in the same second
  as the preceding build's `.pyc`. The cache stays "valid" and the import
  serves the old version while `tomllib` reads the new one. It fired
  naturally on 0.55.3 **and again on 0.55.4**. The test now reads both
  files as text — comparing two files should never route one through an
  import cache.
- **`probe` blamed you for its own confusion.** Arity is checked with
  `bind()` *before* the call, the same rule `cli.main` follows, so
  "you called it wrong" and "it is broken" no longer read alike. When the
  target itself raises on its first frame, the signature and a
  types-not-arity hint are attached.
- **`probe` could run the wrong repo's code.** It imports by module name,
  and `sys.modules` is per-process — so a second repo containing `m.py`
  got the first repo's module, executed its code, and silently lost
  `raised_at`. Found by probe's own new tests running two fixtures in one
  session, which is exactly a long-lived MCP server probing several repos.

### Changed

- CLAUDE.md no longer hardcodes entity and knowledge counts. It claimed
  "635 entities, 45 knowledge entries" against a real 1,068 and 123, and
  **two independent reviewers quoted it as current fact** — in the file
  that states lesson 11. It now points at `memway summary .`.

## [0.55.4] - 2026-08-16

Theme: **comment rot rides the commit.**

**From this release, comment rot is caught at the commit that causes it;
the pre-existing backlog is the last one.** The detection has worked
since 0.53 — it was only ever reported to a queue somebody had to
remember to visit, so 49 rots accumulated on this repo while every
release closed clean. The backlog is now finite and shrink-only.

### Added

- **`verify_change` reports `rotted_comments`**, mirroring
  `staled_knowledge`: a change invalidates two things — the knowledge
  attached to the map, and the comments sitting in the source. Both are
  now named at the moment the author still has the reasoning in their
  head.
- **Scoped to `changed_ids` only.** The backlog stays in `memway
  attention`. A commit-time report that listed all 49 pre-existing rots
  would be scrolled past within a week — `attention` read 43 when 3 were
  actionable and stopped being worked, and that lesson cost a release.
  `attention` is a queue you visit; this is an alarm, and an alarm that
  fires on other people's work is not an alarm.
- **The pre-commit hook greps for both sections.** It named
  `STALED KNOWLEDGE` alone, so a rot section would have printed into a
  pipe nobody reads. `hooks install` upgrades an existing block in place —
  the 0.55.0 marker machinery, cashed in.
- **Three outs, printed as actions**: edit the comment, record a
  `confirm` ("read it, the logic moved, the comment is accurate"), or
  accept it and let it ride to `attention`. Never blocking. What the
  ceremony refuses is walking past it in silence.

### Fixed

- **`attention` truncated comment rot silently.** It returned
  `rot[:limit]` with no total while `markers` — built into the same
  payload three lines away — had shipped `marker_total` all along. This
  repo reported "comment rot: 20" for releases; **the true backlog is
  49**, and two independent readers on one day reported 20 and 10
  believing both were totals. Now `comment_rot_total`, printed as
  `comment rot: 49 (showing 20)`.

### Modules are re-routed, not dismissed

`rotted_comments` skips module entities at the commit surface only. A
module's logic hash aggregates its contents, so it moves on any change to
the file — a confirm recorded against it stales on your next commit and
the same warning returns forever. An unanswerable warning is one people
learn to scroll past. Modules keep their rot in `memway attention`, where
16 of this repo's 49 backlog entries are module-level.

### One rule, one implementation

The confirm-suppression test lived **inline in three places** — this was
found, not assumed: extracting it as `metadata.rot_is_answered` and
adding the structural pin immediately surfaced a third copy in
`before_edit` that nobody knew about. `attention`, `before_edit` and
`verify_change` now all ask it.

### Dogfood note

The ceremony caught this release. Running `verify-change` before the
commit reported 5 rotted comments and 3 staled entries in 0.55.4's own
diff — including `pre_commit_block_for`, whose docstring still said
"Report what you staled" after the function had started reporting both.
That comment was edited; four others were confirmed on merit.

## [0.55.3] - 2026-08-16

Theme: **the closure idiom.** A function may call a helper it defines
itself, and until now memway dropped that edge.

### Fixed

`_unreachable_target` rule 1 says, in its own docstring, that a
function-local def "cannot be named from **outside** that body". The code
never checked where the caller was: it walked the target's parents, found
a function, and refused the edge — including when the caller *was* that
function. That is the ordinary Python closure idiom, and it is not a
guess: the target is lexically in scope with exactly one candidate.

Measured against three real repositories at pinned shas, ground truth
from stdlib `ast` with real scope tracking:

| repo | recall before | after | closure edges missing |
|---|---|---|---|
| psf/requests @ 8068356 | 97% | 97% | 2 → 1 |
| pallets/click @ cbd7a41 | 68% | 69% | 8 → 2 |
| Textualize/rich @ 9d8f9a3 | 90% | **98%** | 50 → **0** |

60 → 3 across the three. rich carried 50 of them, including
`Tree.__rich_console__ -> make_guide`, called six times inside the very
function that defines it — its core render path showed no edge.

The scope check walks **up** from the caller, so a sibling closure calling
its parent's helper resolves too; lexical scope is inherited, and stopping
at the direct parent would trade one wrong answer for a smaller one.

**Rules 2 and 3 are untouched, deliberately.** 111 of the 184 oracle
"misses" in this measurement are `receiver.method()` attribute calls that
memway refuses to guess. That refusal is the 0.54.3 fix doing its job —
it is what removed 369 false edges — and it is load-bearing. The recall
numbers above are *lower* than the tool's true accuracy for exactly that
reason.

### Pinned, in two layers

- **Fixture** (`test_units_core2.py`, every run): caller-is-encloser, plus
  a sibling closure, plus a foreign module that must **still** be refused.
  Falsified in both directions — reverting the scope check fails it, and
  so does removing guard 1 altogether.
- **Corpus floors** (`test_corpus_recall.py`, `pytest -m network`): the
  three repos above at pinned shas, with a recall floor and a
  closure-miss ceiling each. Deselected by default so a normal run stays
  offline; run at release.

Both exist because neither suffices. Every hand-built fixture in this
suite passed on the day the bug shipped — none of them had the caller be
the encloser. Fixtures encode what you thought of; corpora catch what you
didn't.

### Schema and bundles

**`PARSE_SCHEMA_VERSION` does not bump**, and this was verified rather
than reasoned: the cache holds *parse* output, while edge resolution runs
after it. Indexing a repo under 0.55.2, upgrading in place without
touching `.coord`, and running a plain `memway index` produces the new
edge from the warm cache. No half-migrated-map trap here.

Existing maps do keep their old edges until re-indexed, and nothing
prompts for it — an upgrade is not a code change, so freshness stays
silent. **Run `memway index` once after upgrading** to pick up closure
edges.

**Bundle regeneration is NOT required.** Improved edge derivation
invalidates no stamp and no knowledge: hashes are computed from source,
not from edges, so every note in the gen-2 bundles remains exactly as
fresh as it was. The published `itsdangerous`, `flask` and `httpx` maps
stay true at their shas — they simply lack closure edges in code that
uses the idiom. They will pick them up at their next rebuild.

## [0.55.2] - 2026-08-16

Theme: **one rule, one implementation - and every sentence derives.**

### The queue told the truth about everything except itself

`memway attention` reported **43 stale knowledge entries** on this repo
when **3** needed answering. The other 40 were entries somebody had
already replaced, with the replacement sitting directly above them in the
same channel. `attention` hand-counted `en.get("stale")` across every
entry instead of asking `unsuperseded_stale`, so superseded history read
as a live warning - the 0.54.2 lesson (*superseded is not stale*)
reappearing in the one surface named "the queue".

Ambient `knowledge_lag`, reading the same bytes through the same rule,
said 3 the whole time. Two surfaces, one repo, one number, two answers.

- `attention` routes through `for_display` / `unsuperseded_stale`
- the four scattered `from .metadata import ...` sites in `query.py`
  collapse into one module-level import
- **the no-reimplementation pin extends** from `stamp_for` to
  `unsuperseded_stale` and `for_display`: no module may hand-roll
  staleness or reading order again

### Fixed

- **The migration message named the wrong versions.** It printed
  `(v1 -> v2)` as a constant while `SKETCH_VERSION` was **3** - announcing
  a migration nobody was performing. Both ends now derive from
  `stored_sketch_version()` and `SKETCH_VERSION`.
- **The version handshake was silent on the crash it exists for.**
  `version_drift()` ran after the tool call and inside the same `try`, so
  an old server dying on a re-indexed map returned a bare `{"error": ...}`
  and never said *restart your MCP server* - and in that state every call
  raises, so no successful response was ever left to carry it. Observed
  live on this repo. The notice is now computed before the call and
  attached to both branches.
- **The behind-count counted from the wrong baseline.** The pre-commit
  hook indexes while HEAD is still the previous commit, so
  `indexed_at_sha` names the commit *before* the one the map describes.
  Invisible while the tree matches; the moment code moved, one change
  reported as `behind: 2`. `baseline_for_tree()` now finds the commit
  whose content the map actually is, bounded to 25 commits with a
  fallback to the recorded sha.
- **A typo got the whole manual.** `memway freshness .` printed the full
  quickstart and never named the word it did not recognise. Unknown
  commands now get one line on stderr plus a `did you mean` suggestion;
  bare invocation still prints the map.

### Not a defect

`summary`'s `knowledge.superseded` counts **coordinates retired by a
rename** whose knowledge migrated to a successor - a lineage concept,
pinned by `test_census_superseded_vs_orphaned`. It is unrelated to
`for_display`'s per-entry `superseded` flag, and its `0` on this repo is
correct. One word, two meanings, two surfaces: the conflation produced a
defect report against correct code, and is recorded on the coordinate.

## [0.55.1] - 2026-08-16

Theme: **the map earns its bytes.** 0.55.0 made the map ride inside every
commit, which made its size a recurring cost rather than a one-off. Two
changes, measured on flask (1,816 entities, 5,556 commits) before and
after:

| | 0.55.0 | 0.55.1 |
|---|---|---|
| tracked map | 7.70 MB | **3.62 MB** (-53%) |
| `coordinates.json` | 3.2 MB | **2.05 MB** |
| bytes/entity | 1,818 | **1,184** |
| tracked files | 7 | **6** |
| per-commit diff | 21 ins / 13 del | 20 ins / 12 del |
| commit latency | 0.66 s | **0.63 s** |
| 59k-entity extrapolation | 102.3 MB | **66.6 MB** |

### Changed

- **The parse cache is no longer tracked.** It lived in `.coord/index/`
  and inherited "tracked" from its address rather than its nature - 2.9 MB
  on flask, **38% of everything memway committed**, for bytes any machine
  rebuilds in seconds. It moves to `.coord/cache/`, where the derived
  taxonomy already ignores its family. Existing repos are migrated on the
  next index: the file is moved AND `git rm --cached`'d, because moving
  alone would leave git carrying the old path in every future commit. The
  migration announces itself on one line; a tool that quietly changes what
  your next commit contains has taken a decision nobody offered it.

- **Sketches are stored base64, not as 48 JSON integers.** Benchmarked on
  800 real sketches: base64 386 B/entity, hex 578, raw integers 728 - so
  47% off the largest field in the map, which was 55% of every entity
  record.

  **Serialization only: it is decoded back to a list at load.** An AST
  sweep found TWENTY reads of `.sketch` across `lineage.py`, not just
  `sketch_jaccard` as the code reads at a glance, and several are `zip()`
  and `len()`. Those accept a string without raising and compare
  CHARACTERS - a compact form left in memory would not crash, it would
  silently return wrong similarity for every pair. Base64 decodes slower
  than hex (5.7 vs 4.2 us), which would matter per COMPARISON; it happens
  once per entity per load, ~10 ms for all of flask.

  `SKETCH_VERSION` 2 -> 3. A stored array still loads - absent-or-old
  reads as the prior generation, never as current. Third application of
  that pattern, after the generation stamp and the raw-edge field filter.

## [0.55.0] - 2026-08-16

Theme: **automation you don't have to think about.** Neither of these is
a correctness bug. Both are daily grit - the kind that makes someone
quietly stop using a tool, which is worse than a crash because nobody
files it.

### Fixed

- **An existing hook install is UPGRADED, not left alone.** `plan()`
  returned "already has the memway block - leaving it" for any file
  carrying the markers, so an install could never receive a changed hook
  body. Everything below would have shipped installed and inert for every
  repo that had run `hooks install` before. Found by running the upgrade
  on memway's own repo before committing, rather than on a fresh fixture
  where the question cannot arise. Content outside the markers is
  untouched, and a second run reports "already current" and rewrites
  nothing.

- **The map rides inside the commit that changed the code.** The
  post-commit hook re-indexed AFTER the commit, leaving `.coord` dirty,
  so every change cost TWO commits - and `git checkout` refuses to switch
  branches with a dirty tree. That blocked a merge twice in one session
  on 2026-08-16, for the person who wrote the hook.

  The pre-commit hook now reports what you staled, re-indexes, and
  `git add .coord`. **Order is the design and it is not the obvious one:**
  the staleness report runs FIRST, against the pre-index state, because
  that is the only moment it can see what your change invalidated. Index
  first and the report goes quiet - still installed, silently useless.

  It stages `.coord` and nothing else. Never `git add -A`: a hook that
  stages the user's unrelated work has taken a decision nobody offered
  it.

- **Freshness compares TREES, not commit shas.** The enabling fix, and it
  had to come first: a map indexed during pre-commit records the PREVIOUS
  HEAD, so a sha comparison reports `behind: 1` on a map that describes
  the code exactly.

  Comparing content makes a whole class disappear. 0.53.2 had to add a
  commit-counting rule that excludes `.coord` purely to stop the warning
  firing forever on the workflow this project recommends; bisect, rebase
  and fresh worktrees are now honest for free, because they move commit
  shas and leave the tree alone. Maps with no tree recorded fall back to
  the commit path - absent means old, and old means fall back, never
  fail.

- **A drifted MCP server says so.** An MCP server keeps the code it
  started with, so upgrading memway underneath a live agent leaves it on
  the old build. Silently, usually: notes written through a session-old
  server came back stale while the same notes through the CLI landed
  fresh. Once, not silently at all - 0.54.3 added a field to `RawEdge`
  and the old server raised `unexpected keyword argument` on every call.

  Tool output now carries `server_version_drift` when the installed
  version has moved past the running one. Once per session, because the
  condition cannot resolve while the process lives and repeating it
  trains the reader to scroll past. **It never refuses:** a stale server
  that answers beats a dead one mid-session.

## [0.54.3] - 2026-08-16

Theme: **uniqueness is not certainty.**

### Fixed

- **A fifth of all call edges pointed at the wrong thing.** Edge
  confidence was assigned before anything checked HOW a reference
  resolved: `conf, how = 0.95, "exact"` fired the moment `resolve()`
  returned anything, and `resolve()` falls back to matching the last
  segment of a name. So a short name with exactly ONE definition anywhere
  in the index became the target for every call of that name - including
  calls to methods the repo never defines.

  Measured on memway's own map: **369 of 1,627 call edges (23%)** landed
  on an entity whose short name is a stdlib method. Two absorbed 295 of
  them - a module-level test helper named `get` collecting every
  `dict.get()` in the package, and a one-line stub class inside a test
  function collecting every `Path.read_text()`. They were scored 0.95,
  above the <0.7 line the grounding block warns about, so nothing flagged
  them. That was the hairball in the rendered graph, and it inflated
  `fan_in` and blast radius everywhere those nodes were reached.

  Three reachability rules now reject a name-only match that cannot be
  the callee. All three are facts about scope, not lists of names - a
  stdlib name list would be endless, language-specific, and wrong the
  moment a repo legitimately defines `get`:

  1. **Function-local targets.** A class or def inside a function body
     cannot be named from outside it.
  2. **An attribute call is not a module-level function.** `d.get(x)`
     cannot be `def get` at module scope. The parser knew the call was
     written `receiver.name(...)` and discarded it; `RawEdge.via_attr`
     carries it now.
  3. **Production code does not call test helpers.** The same asymmetry
     metrics already relies on. D11b could never help here: it only fires
     on AMBIGUOUS names, and these had exactly one definition - no
     competition was being read as high confidence.

  Result on this repo: `read_text` **136 -> 0** incoming, `get`
  **159 -> 19** (all remaining callers inside `tests`), 850 unreachable
  edges dropped, and the top of the fan-in table is real code again.

  Four resolution tiers match on a short name. The first pass guarded
  two, and the rest kept feeding the same false hubs until re-measuring
  found them - so a structural test now walks `build()` and fails if ANY
  short-name comprehension lacks a guard.

- **Calls into imported modules are qualified, not guessed.** The
  receiver was discarded, so `subprocess.run(...)` became a bare `run`
  and matched whatever unique `run` the index held - 77 call sites
  landing on `Harvester.run`, the second-densest hub in the graph.

  The parser now collects every name bound by an import and qualifies the
  ref. Qualifying rather than dropping is the point: `query.summary()` has
  the same shape and IS ours, so it resolves to `memway.query.summary`
  and gets a BETTER edge than before, while `subprocess.run` resolves to
  nothing and correctly disappears.

  That was only half of it. The inherited-guess tier took the dotted ref,
  threw the prefix away and matched the last segment against every
  entity - putting the edge straight back. It exists for inheritance
  dispatch, where the prefix always names a class this repo has, so it is
  now gated on the prefix resolving.

  `Harvester.run` **61 -> 5** incoming. Across the whole release,
  **2,901 -> 2,554 edges**, and every entry at the top of the fan-in
  table is real code. `Indexer.resolve` keeps its 109 because those
  genuinely ARE `Indexer` instances - verified by reading the receivers.

### Changed

- `PARSE_SCHEMA_VERSION` 4 -> 6: `RawEdge` gained `via_attr`, so cached
  edges must be re-parsed. **The first index after upgrading rewrites the
  graph** - expect edge counts to fall, and the fall is the fix.

## [0.54.2] - 2026-08-16

Theme: **the map never misleads silently.**

`freshness.py` wrote the principle down for maps - *"the map may lag; it
must never lag SILENTLY"* - and enforced it by making every read say so
on the way past. Knowledge got the detection and none of the telling.
0.54.1 shipped a workflow rule saying "supersede what your change
staled" and its own author broke it within the hour, twice in one
evening, with the tool installed and the rule loaded, because nothing
ever said which coordinates had gone stale. A rule that depends on recall
is the failure this project exists to fix.

### Added

- **Stale knowledge warns like map lag does.** Every read carries
  `knowledge_lag` - `"N coordinates hold stale knowledge - memway
  attention"` - on the same three JSON surfaces as `map_lag`, and as one
  printed line on the CLI. Ambient, unasked-for.

  It uses the RING RULE, so superseded history never counts: a repo that
  has answered every stale note reads silent even though the old rows are
  still on disk. memway's own map holds 23 such rows and reports nothing.
  A warning that fires forever is not a warning.

- **A `pre-commit` hook, the fourth.** Prints the knowledge your staged
  work invalidated, and **exits 0 always** - by `|| true` and by
  construction. This is the one that fires with zero memory required.

- **CLI doors for `summary`, `before-edit` and `verify-change`**, which
  existed over MCP and `--json` only. `verify-change` is the one that
  mattered: the pre-commit hook had nothing readable to call.

- **A surface-parity test.** Every MCP tool must have all three doors or
  sit on an exemption list *with a reason* (`meta` is a write; `probe`
  executes user code). Fixed instance-by-instance this recurs - the day
  `attention` was fixed, three other tools were still missing a door.

- **A flagship-identity test** asserting title, header and label against
  the derivation. Honest scope, stated in its docstring: it closes
  constant-drift permanently and cheaply, and will **not** catch the next
  dead toggle.

### Changed

- **The knowledge panel reads newest first, and marks history as history.**
  Entries are append-only, so the file runs oldest -> newest and the panel
  rendered it straight through: on a coordinate whose ring said FRESH, the
  first thing a reader saw was an entry marked STALE - the very one the
  ring rule had discarded. Ring and panel contradicted each other on
  screen, and the truth was below the fold.

  `metadata.for_display` is now the one reading order, used by `show`,
  `before_edit` and the map/console payload. Non-deciding entries carry
  `superseded` and render quieter: **superseded is not stale.** Stale is a
  warning - the code moved and nobody answered. Superseded is history -
  somebody answered, and this is the older answer. Rendering them alike
  taught readers to ignore both.

  `unsuperseded_stale` no longer derives "newest" from list position; it
  reads the flag. A positional rule would have inverted silently the
  moment the display order flipped, calling a coordinate fresh on the
  strength of a note somebody had already replaced.

  Found by a human looking at the panel and asking which end was which.
  The tell that newest-first is right: six superseding notes on this repo
  said "supersedes the note BELOW it" - true of the file, false of the
  screen. The reorder made all six true without editing a word.


- **Project name is derived, not assumed** - `pyproject.toml
  [project].name` -> `package.json name` -> git remote basename ->
  directory last. First in chain wins even when they disagree:
  deterministic beats clever. memway's flagship map read `coordsys-v49`,
  its pre-rename directory; it now reads `memway`. Required **zero**
  template changes, which is the check that 0.54.1's title/header
  unification was done right.

- **Workflow rule 4 reworded**: you are now told without asking, so the
  rule says what to do rather than what to remember.

- `verify_change` runs under `read_only()`. Inert today - the loader
  suppression already makes it a pure read - but it protects the whole
  path rather than the two loaders that happen to be on it.

- **Superseding heals on the first try.** `meta` stamped from the STORED
  index, so a note written after an edit but before a re-index carried the
  pre-edit hash and was **born stale** - it healed nothing, and the
  coordinate stayed coral. That is the exact sequence the pre-commit hook
  puts you in, and nothing tells you to re-index first. `stamp_for` now
  derives the hash from the WORKING TREE, through the same in-memory index
  path 0.54.1 added, on all three write paths (CLI, MCP, console) via the
  one function they already shared.

  The two "corals -> 0" runs on memway's own map earlier that day worked
  only because their author happened to re-index first, out of habit
  rather than instruction. `tests/test_never_silent.py` pins the sequence
  end to end: edit -> hook names it -> supersede -> index -> `stale: False`.

  A ref that no longer resolves in the working tree is REFUSED with a
  message, not stamped against a ghost: such a note could never be fresh
  and could never be superseded. Write scope is unchanged and asserted -
  a meta call still touches exactly one file.

- **`init` writes `.coord/.gitignore`.** A fresh `memway init` plus
  `git add -A` staged `.coord/cache/*.pkl` - binary blobs that change on
  every index and conflict on every merge. It encodes the derived-tier
  taxonomy where git can act on it: `cache/`, `evidence/`, `log/` and
  `versions/` ignored; `meta/`, `lineage/` and `docbindings.json` stay
  tracked. Inside `.coord`, never the user's root .gitignore - editing
  that would be the same trespass as rewriting their CLAUDE.md.

### Upgrading

**Restart your MCP server after upgrading.** It is a long-running process
and holds the code it started with, so an agent session keeps the OLD
stamping behaviour until it restarts - notes written through
`memway_meta` will still be born stale even though the installed memway
is fixed. Found the honest way: two superseding notes written through a
session-old server came back stale, and the same notes written through
the CLI landed fresh. Nothing in the tool says this yet.

The CLI, the console and a freshly started MCP server are unaffected.

### Not built

- **The CI gate** (fail a PR when `staled_knowledge` is non-empty) stays
  on the 0.55 collaboration board, where the rest of the multi-author
  story lives.

## [0.54.1] - 2026-08-16

Theme: **close the loop.** The tool detected staleness perfectly and never
told you at the moment you caused it.

### Fixed

- **`verify_change` now names the knowledge your change staled**, with the
  coordinate, qualname, **channel** and note text. It is the step the
  workflow rules send you to after an edit, and it reported blast radius
  and tests while saying nothing about the notes the edit had just
  invalidated - so the loop never closed and staleness was found later, by
  whoever happened to open a map.

  Channel is not decorative: superseding only heals when the fresh entry
  lands in the SAME channel, so a report omitting it sends the reader to
  write an entry that changes nothing.

  Measured on this repo: five notes on the flagship map went stale during
  0.54.0 and sat coral on memway.io until someone looked. All three
  workflow rules were followed and it still happened.

- **The report is computed against the WORKING TREE, not the stored
  index.** At the moment you would ask - edited, not re-indexed, not
  committed - the stored index still holds the old hashes and reports
  everything fresh. `tests/test_close_the_loop.py` pins that exact state
  and asserts `show` and `verify_change` DISAGREE there, because one reads
  the map and the other reads the tree.

- **`verify_change` is a pure read.** It wrote five files - the index, the
  edges, the parse cache and both pickles. That was a documented exception
  ("THE ODD ONE OUT"), recorded by a test that said what to do if it ever
  changed: move it under the read fence. 0.54.1 does exactly that. After
  0.54.0 a read that re-indexes could perform the sketch migration and
  announce it to a stdout a `--json` caller never displays.

  Both cache-warming loaders had to be suppressed; fixing only the first
  left `edges.pkl` behind, which is how the earlier leaks in this file
  survived.

- **A new query cannot skip the fence.** `test_every_json_query_is_enrolled_
  in_this_fence` compares `QUERIES` against `READS` and names what is
  missing. A count would not have caught the original: the table had the
  right number of entries, just not the right names.

- **`'X' is ambiguous - N entities match'`** replaces the false
  `no entity matches 'X'`, listing the qualnames the resolver already had
  in hand, and failed lookups now exit nonzero from the CLI (they exited
  0, so a script could not tell a miss from a hit). Measured on the
  published 0.54.0 wheel: `get_signature` matched 5 entities in
  itsdangerous, `save` matches 3 here, and both answered "no entity
  matches" - a false negative that sends an agent to grep.

- **The console banner survives redirection.** Python block-buffers a
  non-TTY stdout while the server blocks in `serve_forever`, so
  `memway console > log` produced zero bytes with the server up and the
  single-session token - which exists nowhere else - was unobtainable.

### Fixed

- **Every generated map's browser tab claimed to be a map of
  `itsdangerous`.** The title was hardcoded in `viz_template.html` and
  `render` never overrode it, so this shipped in the wheel: two maps built
  by the published 0.54.0 wheel for unrelated repos both carried it, and
  on memway.io the string `itsdangerous` appeared exactly once in 761KB -
  in the tab - while the body was memway's own 909 entities. A leftover
  from when the flagship map really was itsdangerous.

  Fixed by unifying the source, not by editing the string: `viz.map_label`
  is now the one derivation, and the header and the title are two uses of
  one call. They can no longer drift apart, which is the actual fix.

  **The trade, stated plainly: the tab now reads `coordsys-v49` - this
  repo's directory name - instead of `itsdangerous`.** That is a strict
  improvement (wrong-but-ours beats false attribution) and it is not the
  end state. The label is still built from the directory; 0.54.2 replaces
  that with a real project-name derivation at this single source, after
  which it reads `memway`. Anyone reading between the two releases should
  file the stale name as known, not as new.

  Found by a human looking at a browser tab. Nothing automated caught it,
  because a wrong constant is not a wrong behaviour: the payload, airgap
  and executed-predicate tests all pass on a page whose tab lies. The
  regression test asserts title and header against `map_label` rather than
  against any literal, so it stays true when 0.54.2 changes the answer.

### Added

- **`attention` on all three surfaces.** It was MCP-only: not a `--json`
  query, and `memway attention` printed the usage banner. Every release in
  this project is driven from the CLI, so the one question that finds
  staled knowledge repo-wide could not be asked by its own author.

- **A fourth workflow rule**, emitted byte-identically to `AGENTS.md`,
  `CLAUDE.md` and `GEMINI.md` through the marker-block upgrade path:

  > If your change staled knowledge, supersede it before you finish.
  > `memway_verify_change` names what you invalidated; write a fresh entry
  > in the same channel. Superseding never deletes - the old entry stays
  > as history.

### Changed

- `Indexer.index(persist=False)` computes in memory and writes nothing -
  the parse-cache refresh is the only write the method makes on its own.
  Keyword-only, defaulting to today's behaviour, because 41 callers
  depend on it.
- The ring rule moved to `metadata.unsuperseded_stale` and returns rows;
  `viz.has_unsuperseded_stale` delegates. The ring and the staleness
  report must never be able to disagree about what "stale" means.

## [0.54.0] - 2026-08-16

### Fixed

- **Indexing is deterministic. It was not, and everything downstream of
  that assumption was quietly untrue.** `_sketch` hashed token shingles
  with builtin `hash()`, which Python randomizes per process - and
  sketches are PERSISTED, in both `coordinates.json` and the parse cache.
  Two fresh clones of one commit produced byte-identical maps except
  `sketch`, which differed on **888 of 888 entities**.

  Two consequences, both measured rather than argued:

  1. `git diff --exit-code .coord` could never mean "the map is stale",
     because every index rewrote the whole file. No CI gate was possible.
  2. `sketch_jaccard` is the largest single term in lineage's rename score
     (weight 0.30) and `sketch_containment` is the ONLY signal behind
     split/merge detection. Across processes - which is every real rename,
     since the old sketch comes off disk and the new one is computed now -
     both were noise. Isolated experiment, same repo, same commits, only
     the seed varied: the default run recorded `deleted m.compute_totals`
     with `author="auto"`, orphaning its knowledge behind a confident
     verdict; with the seed pinned on both runs, the same edit was
     recorded as a reviewable link. **Randomization turned "flagged for a
     human" into "silently deleted."**

  Now blake2b at `digest_size=6`, which is exactly the 48 bits the
  permutation already masks to. Benchmarked first: crc32 was faster but
  32-bit, and adler32 mixed so poorly it lost distinct values (4,809 vs
  4,821 on a real 6,880-shingle corpus). End-to-end indexing cost on the
  888-entity repo is unchanged - 2.74-2.79s against 2.78-2.86s before,
  inside the noise.

### Changed

- **`PARSE_SCHEMA_VERSION` 3 -> 4**, so no warm cache can replay
  randomized sketches, and **`sketch_version` is now stamped in
  `manifest.json`** (additive; an absent stamp reads as generation 1,
  never as "current" - a pre-0.54 map must not be able to claim
  comparability it does not have).

- **A signal that could not be measured is excluded, not scored zero.**
  The first index after upgrading is the dangerous state: old sketches on
  disk, new hash in the code. Passing 0.0 for the unmeasured Jaccard
  would multiply every rename score by 0.70 and move all the thresholds -
  reproducing this very bug, one layer quieter. Instead that index drops
  the term and renormalizes, skips split/merge (which has no fallback
  signal at all), downgrades terminal deletions to `pending-review` so
  they land in `memway attention`, and says all of this on stdout.

### Upgrading an existing map

Plainly: **the first `memway index` after upgrading rewrites every
sketch**, and that one run is the migration. Expect a full-file diff of
`coordinates.json` once - after that, two people indexing the same commit
get identical bytes.

**Lineage verdicts from before the bump are not comparable with ones
after it.** The migrating index says so while it runs, and files anything
it cannot rule out as `pending-review` rather than asserting a deletion.
Maps obtained with `memway pull` are in the same position; the published
registry bundles need regenerating under 0.54.0.

## [0.53.3] - 2026-08-16

### Fixed

- **`memway viz --force` was documented and rejected.** The flag is printed
  in viz's own usage line, and typing it produced
  `--force applies to 'pull' only`, because the pre-dispatch flag table
  mapped each flag to exactly ONE owning command and pull claimed `--force`
  first. Flags now declare a tuple of owning commands, and `cmd_viz`
  receives it.

  The reason nothing caught it: the help text lived in a docstring and the
  flag table lived inside `main()`, and no test had ever compared the two.
  `tests/test_cli_usage.py` now reads the flags back OUT of the shipped
  usage text - 12 flags across 7 commands, parsed, not hand-listed - and
  requires every one to be receivable by the command it is printed under,
  by either route (lifted into a keyword, or parsed by the command itself).
  A flag on the wrong command still fails, which the same file pins.

- **Hooks pinned a bare `memway` and could silently do nothing.** A git hook
  inherits whatever environment invoked git - a GUI client, an IDE, a shell
  that never activated the venv - so `memway index . --if-stale` resolved
  only when PATH happened to be right, and the `|| true` that keeps a hook
  from ever blocking a commit also swallowed `command not found`. The map
  stopped syncing while `hooks install` reported success.

  `hooks install` now writes the absolute path of the memway performing the
  install, with two comment lines in the hook saying where it came from and
  what happens if it stops resolving. Proven by execution, not by reading
  the file: `tests/test_freshness.py` runs an installed hook under
  `env -i PATH=/usr/bin` and requires the map to move.

  Found while writing that test: resolving the interpreter path walked out
  of the venv entirely, since `bin/python` is usually a symlink to the base
  install - so the first version of this fix confidently pinned a DIFFERENT
  memway at a different version. It no longer resolves.

- **One test/source rule, enforced across the whole package.** `verify.
  is_test_entity` was promoted to the canonical rule in 0.53.1, and the
  structural test guarding it scanned two modules: the two being edited at
  the time. Four copies of the crude `"test" in path.lower()` heuristic
  survived it - in `indexer`, `harvest` (twice) and `metrics` - which
  disagree with the real rule on `test_helpers/`, `contest.py` and every
  `foo_test.go`. All four now call the shared rule, and the structural test
  walks every module in the package by AST and names what it scanned, so a
  new module cannot pass by being absent from a hand-written list.

- **A coordinate can return to amber once someone re-reads the code.** The
  map's ring was `knowledge.some(k => k.stale)`, and entries are append-only
  and never deleted, so a coordinate that went coral once stayed coral
  forever no matter how many fresh confirmations were written. Meanwhile
  `attention` used a different rule, suppressing rot whenever ANY confirm is
  fresh, so the map and the queue disagreed about the same coordinate.

  Per channel, the newest entry now decides, in one function
  (`viz.has_unsuperseded_stale`) reached by one JS helper (`ringStale`) that
  both ring sites call. The rule was previously inlined at two sites, which
  is the shape that let 0.53.2's behind-count ship without the exclusion the
  dirty check already had.

  Measured on memway's own map: 12 coral rings became 3, and the three that
  remain are exactly the three coordinates nobody has reviewed.

### Changed

- **Comment rot fixed in three places, found by reading rather than by
  tooling.** `query.agent_meta` told every MCP caller its note was
  "body-hash-stamped" - false since stamping was unified on `stamp_for` in
  aa77673, and the inline comment beside it said so correctly the whole
  time. `Indexer` cited "lines 284-295" for the schema-version check, by
  then occupied by `_new_id()` and a class header; it now names
  `PARSE_SCHEMA_VERSION` instead, which cannot drift. `tests/test_cli_units`
  claimed the command layer was 322 lines (947) and carried 11 section
  headers advertising sections that held no tests at all.

## [0.53.2] - 2026-08-16

### Fixed

- **Committing the map no longer triggers a permanent false lag warning.**
  The behind-count now excludes `.coord`, matching the dirty check. memway
  tells you to commit the map, so committing it moves HEAD; `lag()` gated on
  sha equality, so from that moment every read reported the map as one
  commit stale while it described the code exactly. Every repo following the
  documented workflow got a warning that could never be cleared, which is
  precisely the noise the module's own comment warns against.

  `is_dirty()` learned this exclusion weeks earlier. The behind-count was a
  second copy of the same rule and only the first copy was fixed, so there
  is now one implementation, `code_commits_between`, reached by the lag
  warning, `--if-stale` and the hooks alike.

  An unreachable recorded sha (rebase, force-push, shallow clone) now
  reports instead of going quiet: "cannot tell" is not "nothing changed",
  and collapsing them would let a rewritten history read as current.

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
