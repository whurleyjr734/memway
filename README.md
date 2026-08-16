# Memway

**A memory layer for codebases.** Stable coordinates for every function and class — identity that survives renames and refactors — with knowledge attached to those coordinates so that what your agents *learn* about your code outlives the session that learned it.

> A cache answers what the code is. A memory knows what we've learned about it.

Every serious code tool — indexes, language servers, code search, RAG pipelines — rebuilds *what the code is* on demand. None of them holds *what was discovered about it*: why a constant has its value, which incident a guard prevents, what a reviewer confirmed after reading it carefully. That knowledge lives in chat sessions, PR threads, and heads — and it evaporates. memway gives it an address, checks it for staleness against the code itself, and hands it to the next agent at the moment it's about to matter.

## Quickstart

```bash
pip install memway        # zero runtime dependencies (stdlib only)
cd your-repo
memway setup .
```

`setup` does three things, all idempotent: builds the map (`.coord/`), writes `.mcp.json` so agents pick up the ten memway tools, and writes the three-rule workflow file to `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` — byte-identical copies from one template, because a client reads the filename it knows and ignores the rest. Then restart your agent in the repo and ask it:

```
What does this repo know?
```

The answer will be empty. Here's why that changes.

## Or inherit a map you didn't build

```bash
pip install memway
memway pull flask --into ~/maps/flask
memway console ~/maps/flask
```

That directory contains no Flask source — a `.coord/` and nothing else — and the console opens on 1,816 entities with the knowledge already attached, including a note on `AppContext.push` explaining the context-leak fix behind issue #6123 and why the cross-thread exception it leaves in your logs is harmless. Someone indexed Flask once; you inherited the reasoning.

Published maps live at [memway-maps](https://github.com/whurleyjr734/memway-maps) — today `itsdangerous`, `flask`, and `httpx`, each pinned to the upstream commit it was indexed from. See [The registry](#the-registry) below.

## The three rules

`setup` writes this to all three filenames. It is the entire configuration — the
mechanism behind Phase B below, and the reason a map fills itself instead
of sitting empty:

```markdown
# Project rules

This repo uses memway as its memory layer. Each tool below is available
over MCP with the exact name given; where a CLI equivalent exists it is
named in parentheses. Use whichever your client supports.

- Before editing any function or class, brief yourself on it with
  `memway_before_edit` (CLI: `memway --json before-edit . <ref>`) and heed
  any attached knowledge. If the call returns an error, do NOT edit -
  resolve the ref first with `memway_at` (CLI: `memway at . <file:line>`),
  or try a bare function name or module.qualname, then retry.
- After completing changes, call `memway_verify_change`
  (CLI: `memway --json verify-change .`) to confirm impact. It reports
  which tests reach your change through the graph; running them is your
  job, not its.
- When a task, design doc, or conversation supplies a REASON a piece of
  code is the way it is (constraints, incidents, tuning rationale), record
  that reason with `memway_meta` (CLI: `memway meta . <ref> notes "<text>"`)
  on the relevant entity - reasons must outlive this session. This is due
  whenever a reason or finding SURFACES, not only when a change lands:
  tasks you decline, block on, investigate, or leave unfinished count too.
  The reason a change was refused is often the most valuable thing to
  record - a constraint strong enough to stop work is exactly what the next
  session needs and exactly what the code cannot say on its own. Capture it
  before you reply.
- If your change staled knowledge, supersede it before you finish.
  `memway_verify_change` names what you invalidated; write a fresh entry in
  the same channel. Superseding never deletes - the old entry stays as
  history.
```

Three properties matter. Tool names are **exact** — agents that have to
guess a name skip the call. The error branch is **explicit**, so a failed
lookup stops the edit rather than silently proceeding without the
briefing. And the write-back rule names *reasons*, not summaries: what
the code cannot express on its own.

A rules file `setup` cannot prove it wrote is **left alone and reported**,
never rewritten. Its own block is delimited by an HTML comment marker;
anything you add below that marker survives every later `setup`. To adopt
an existing hand-written rules file, paste the marker above your content
and `setup` will manage the block above it and keep the rest.

## The experiment this is built on

We gave a fresh coding agent the same task twice on a repo it had never seen: *implement a feature per the design doc*. The doc contained a constraint whose reason the code cannot express — a rate-limit window that must stay at 10 seconds because billing reconciles in 10-second buckets, learned the hard way in an incident.

**Phase A — no configuration.** The agent read the doc, understood it perfectly, implemented it correctly, kept the tests green — and recorded the reason *nowhere*. No comment, no note. Tool-usage log: empty. When the session ended, the "why" died with it.

**Phase B — the three-line rule file `setup` installs.** Same task, fresh agent. It briefed itself with `memway_before_edit` before touching anything, verified impact after, and wrote the constraint — incident number, billing rationale, correct alternative — to the coordinate where the window logic lives.

**Phase C — the trap.** A third agent, provably a different process (session ids logged), was asked for exactly the change that caused the fictional incident: *"widen the burst window to 30 seconds."* Its first words were "I'll help you widen the burst window." Then the rule file routed it through `before_edit`, the map surfaced the dead session's note, and the agent changed course mid-task — explained the incident, proposed the safe alternative, and put the decision back in human hands.

Unconfigured agents execute perfectly and remember nothing. Three lines of configuration make the memory fill itself. And inherited memory then prevents the recurrence of the exact incident it records. That loop — write-back, inheritance, interception — is the product.

## How it works

- **Coordinates.** Each entity gets a content-derived id: `C-` plus the first six hex of `sha256(qualname)`. The same function has the same address in every clone, on every machine, forever — which is what makes a map transplantable at all. Lineage detection (multi-signal: name, body similarity, structure, signature, location) carries identity through renames and moves — measured at 100% on a 172-link ground-truth set at 0.998 confidence, 0.995+ precision across four foreign repos (requests, django, flask, click) with zero tuning.
- **Three-tier hashing.** Body, logic (docstrings and comments stripped), and shape hashes distinguish cosmetic edits from behavioral ones. Reword a docstring and your notes stay fresh; add a `raise` and they flag. This drives everything: staleness detection, metric memoization, certain-tier lineage.
- **Self-auditing knowledge.** Notes, docs, design references, and confirmations attach to coordinates across six channels, each stamped with the code's logic hash at write time. When the logic changes, the entry is *flagged stale* instead of silently lying — never deleted, because "this used to be true" is itself information. Comment-rot detection does the same for source comments; a confirm channel lets a reviewer attest "correct as of this logic" and silences warnings until behavior actually changes.
- **Agent tools (MCP).** Ten tools: `summary` (repo shape + a census of everything the map remembers), `show`, `at`, `lineage`, `before_edit`, `verify_change` (test selection from the edge graph), `dig` (on-demand history for one entity), `probe` (runtime evidence with confidence provenance), `meta` (write-back), `attention` (repo-wide problems queue). Small primitives that compose, not a menu.
- **Token economy.** The map returns coordinates *into* files, never copies *of* them — measured 56–472× token savings versus reading source. Agents use the map to aim surgical reads.
- **Transparent by construction.** Everything persisted is plain, greppable JSONL under `.coord/`. The included usage log is local-only and reference-only: no telemetry, no phone-home, ever — and as of 0.51.1 that extends to rendered output: `viz` and `console` emit HTML that makes **zero network requests** (d3 is vendored and inlined, fonts are system stacks), so a map of your private source tree never announces itself to a CDN and works on a plane. Commit `.coord/` and your teammates — and their agents — clone the repo *with its memory*. Always commit `.coord/meta`: it is authored, unrecoverable, and tiny. `.coord/index` is derived and regenerates on clone — commit it on small-to-mid repos so a clone arrives instantly useful, gitignore it at large scale (see limits). Authored is precious; derived regenerates.

## The commands

Fourteen, all `memway <command> <repo> [args]`:

| | |
|---|---|
| `setup` | one-command onboarding: map + `.mcp.json` + `CLAUDE.md` |
| `init` / `index` | build the map / refresh it incrementally |
| `harvest` | mine docstrings, git history, and design docs into the channels |
| `at` | `file:line` → entity — the handoff from a grep hit |
| `show` | entity dossier: signature, edges both ways, knowledge with staleness |
| `meta` | attach knowledge at a coordinate (`--author`) |
| `lineage` | identity history through renames and moves |
| `dig` | mine one entity's history: commits, PR bodies, release tags |
| `evidence` | read cached commit/PR bodies (`--clear` removes only derived) |
| `viz` | the real map as one self-contained interactive HTML file |
| `console` | the map served live, read tools as buttons (127.0.0.1 + token) |
| `pull` | fetch a published map into `.coord/` |
| `mcp` | run the MCP server |
| `--json <query>` | structured output: `summary`, `at`, `show`, `before-edit`, `lineage`, `dig` |

`dig` deserves a note: it returns **candidates** and refuses to judge them. Deciding whether a commit message carries a real reason or merely restates the diff is judgment, and a tool that automates it produces confident garbage. It never writes to `.coord`, never scores, never gates.

## The registry

```bash
memway pull httpx --into ./vendor-maps/httpx
```

A bundle is a tarball fetched over the network, so it is treated as hostile until proven otherwise: the SHA-256 checksum must match before anything is unpacked, every member is validated *before* extraction, members must resolve inside the target and live under `.coord/`, and links and devices are refused outright. File modes are normalized rather than trusted, because the stdlib's safe-extraction filter only exists on Python 3.12+ and this package supports 3.10.

`--force` replaces the derived index and **merges** the bundle's knowledge into yours — locally authored entries are never deleted, and dedup is on the exact line, so re-pulling is idempotent. `--replace-meta` is the destructive path and deliberately does *not* imply `--force`: you type both, and typing both is the moment you notice which you asked for.

Every bundle carries a manifest recording `upstream_repo`, `upstream_sha`, the memway version that built it, and the upstream license. Drift is measured against `upstream_sha`, so if your working tree sits at a different commit than the map describes, `pull` says so rather than letting you trust a map for code it never saw.

Maps hold coordinates and commentary, never upstream source, and each carries its upstream project's own license. **Pull requests are welcome** — new maps, and especially better knowledge on existing ones.

## Keeping the map fresh

A map that silently describes last week's code is worse than no map. Three
layers, and only the last one is a guarantee:

```bash
memway hooks install          # post-commit, post-checkout, post-merge
memway index . --if-stale     # the same check, for scripts and CI
```

`--if-stale` compares the sha the map was built from against `HEAD` and a
dirty check, and does nothing when they agree (50-100ms, no writes). Hooks
are **opt-in**: `setup` mentions them and never installs them, and an
existing hook is appended to inside a marked block that `uninstall` removes
exactly.

Neither covers a bisect, a fresh worktree, a hand-edited tree, or a
colleague who never installed anything. So **every read tool says when the
map lags**:

```
note: map indexed at 652f58d, HEAD is f2e6bc3 (7 commits ahead) - run memway index
```

That line is the promise. The map may lag; it will not lag quietly. It was
written because memway's own map sat seven commits behind, with the
re-index rule written down and followed by nobody, and the only symptom was
comment-rot warnings that looked like drifted comments.

## Honest limits

This is a young tool that has been tested hard in a narrow way. Known limits, from our own findings ledger:

- **Scale** is measured to ~59K entities (Django, via the published package): cold index ~3 minutes, incremental reindex ~33 seconds with full memoization. At that scale the derived index grows large (~275MB), so large repos should commit `.coord/meta` (the knowledge) and gitignore `.coord/index` (structure regenerates on clone) — the commit-the-map default is tuned for small-to-mid repos.
- **Python is first-class.** Go, JavaScript, TypeScript and Java parsing is optional (`pip install 'memway[languages]'`, quoted so your shell does not eat the brackets) and thinner. Method signatures are extracted for Go, JavaScript and TypeScript, but not Java. Leading doc comments are lifted for Go and JavaScript only; inline comments and comment-rot detection work for all four. No field declarations are indexed as entities. Staleness is coarser: `logic_hash` currently equals `body_hash` for the tree-sitter languages, so a comment-only edit can flag a note that Python's logic hash would shrug off. Missing grammars produce an explicit warning naming each skipped language, never a silent skip.
- **Cyclomatic complexity is a triage input, not a risk verdict.**
- **Knowledge quality over months of accumulation is unproven.** Staleness flags, the attention queue, and note scoping are the designed defenses.
- **Knowledge is coordinate-scoped, and some knowledge isn't about a coordinate.** A lesson that governs *how you work* — rather than what one function does — has no home in the map today and lives in `CLAUDE.md` instead. A repo-scope channel is the open design question.
- **The registry is small and new.** Three maps, published by us. Nothing about the format is frozen.
- **The usage log's session id tracks server processes, not conversations.**

## Status

Built and dogfooded on itself: the map indexes memway, agents maintain memway through the map, and the lessons in the ledger were written into the map by the agents that learned them. 369 tests. Python ≥3.10, zero runtime dependencies. AGPL-3.0-or-later.

No features are planned until real usage asks for them — the usage log exists so that answer comes from evidence.
