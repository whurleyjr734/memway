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

`setup` does three things, all idempotent: builds the map (`.coord/`), writes `.mcp.json` so agents like Claude Code pick up the nine memway tools, and installs a three-rule `CLAUDE.md` workflow file. Then restart your agent in the repo and ask it:

```
What does this repo know?
```

The answer will be empty. Here's why that changes.

## The three rules

`setup` writes this `CLAUDE.md`. It is the entire configuration — the
mechanism behind Phase B below, and the reason a map fills itself instead
of sitting empty:

```markdown
# Project rules

This repo uses memway (MCP tools prefixed `memway_`) as its
memory layer.

- Before editing any function or class, call `memway_before_edit`
  on it and heed any attached knowledge. If `memway_before_edit`
  returns an error, do NOT edit - resolve the ref first (try a
  bare function name, module.qualname, or memway_at <file:line>)
  and retry before_edit.
- After completing changes, call `memway_verify_change` to confirm
  impact.
- When a task, design doc, or conversation supplies a REASON a piece
  of code is the way it is (constraints, incidents, tuning
  rationale), record that reason with `memway_meta` on the relevant
  entity - reasons must outlive this session.
```

Three properties matter. Tool names are **exact** — agents that have to
guess a name skip the call. The error branch is **explicit**, so a failed
lookup stops the edit rather than silently proceeding without the
briefing. And the write-back rule names *reasons*, not summaries: what
the code cannot express on its own.

If you already have a `CLAUDE.md`, `setup` leaves it alone and prints a
notice — paste the three bullets in yourself.

## The experiment this is built on

We gave a fresh coding agent the same task twice on a repo it had never seen: *implement a feature per the design doc*. The doc contained a constraint whose reason the code cannot express — a rate-limit window that must stay at 10 seconds because billing reconciles in 10-second buckets, learned the hard way in an incident.

**Phase A — no configuration.** The agent read the doc, understood it perfectly, implemented it correctly, kept the tests green — and recorded the reason *nowhere*. No comment, no note. Tool-usage log: empty. When the session ended, the "why" died with it.

**Phase B — the three-line rule file `setup` installs.** Same task, fresh agent. It briefed itself with `memway_before_edit` before touching anything, verified impact after, and wrote the constraint — incident number, billing rationale, correct alternative — to the coordinate where the window logic lives.

**Phase C — the trap.** A third agent, provably a different process (session ids logged), was asked for exactly the change that caused the fictional incident: *"widen the burst window to 30 seconds."* Its first words were "I'll help you widen the burst window." Then the rule file routed it through `before_edit`, the map surfaced the dead session's note, and the agent changed course mid-task — explained the incident, proposed the safe alternative, and put the decision back in human hands.

Unconfigured agents execute perfectly and remember nothing. Three lines of configuration make the memory fill itself. And inherited memory then prevents the recurrence of the exact incident it records. That loop — write-back, inheritance, interception — is the product.

## How it works

- **Coordinates.** The indexer assigns each entity a durable ID. Lineage detection (multi-signal: name, body similarity, structure, signature, location) carries identity through renames and moves — measured at 100% on a 172-link ground-truth set at 0.998 confidence, 0.995+ precision across four foreign repos (requests, django, flask, click) with zero tuning.
- **Three-tier hashing.** Body, logic (docstrings stripped), and shape hashes distinguish cosmetic edits from behavioral ones. This drives everything: staleness detection, metric memoization, certain-tier lineage.
- **Self-auditing knowledge.** Notes, docs, design references, and confirmations attach to coordinates, each stamped with the code's logic hash at write time. When the logic changes, the entry is *flagged stale* instead of silently lying. Comment-rot detection does the same for source comments; a confirm channel lets a reviewer attest "correct as of this logic" and silences warnings until behavior actually changes.
- **Agent tools (MCP).** Nine tools: `summary` (repo shape + a census of everything the map remembers), `show`, `at`, `lineage`, `before_edit`, `verify_change` (test selection from the edge graph), `probe` (runtime evidence with confidence provenance), `meta` (write-back), `attention` (repo-wide problems queue). Small primitives that compose, not a menu.
- **Token economy.** The map returns coordinates *into* files, never copies *of* them — measured 56–472× token savings versus reading source. Agents use the map to aim surgical reads.
- **Transparent by construction.** Everything persisted is plain, greppable JSONL under `.coord/`. The included usage log is local-only and reference-only: no telemetry, no phone-home, ever. Commit `.coord/` and your teammates — and their agents — clone the repo *with its memory*.

## Honest limits

This is a young tool that has been tested hard in a narrow way. Known limits, from our own findings ledger: scale is characterized to ~2.5K entities per repo (larger is unmeasured); Python is first-class while JS/TS/Go/Java parsing is optional (`pip install memway[languages]`); cyclomatic complexity is a triage input, not a risk verdict; knowledge quality over months of accumulation is unproven (staleness flags, the attention queue, and note scoping are the designed defenses); and the usage log's session id tracks server processes, not conversations. The full dogfooding ledger — including the time an agent's recovery instinct was to delete the memory, and what we changed because of it — ships in `docs/`.

## Status

Built and dogfooded on itself: the map indexes memway, agents maintain memway through the map, and the lessons in the ledger were written into the map by the agents that learned them. 133 tests. AGPL-3.0. No features are planned until real usage asks for them — the usage log exists so that answer comes from evidence.
