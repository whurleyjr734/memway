# Project rules

This repo IS memway, and it uses itself. The map in `.coord/` is
committed and live (run `memway summary .` for current counts; this
line used to carry them and they rotted — see lesson 11) — it is not a
scratch artifact, and a stale or damaged map is a real defect. The venv
is `.venv/`; MCP wiring is `.mcp.json` at the root.

## 1. The three workflow rules

- **Before editing any function or class, call `memway_before_edit`**
  and heed the attached knowledge. The 45 entries include defects,
  tuned constants, and load-bearing constraints that the code does not
  state. If `before_edit` errors, do NOT edit — resolve the ref first
  (bare name, `module.qualname`, or `memway_at <file:line>`) and retry.
- **After completing changes, call `memway_verify_change`** to confirm
  impact. In this repo that includes the map itself: re-index if you
  changed parsing, hashing, or entity extraction.
- **When anything supplies a REASON the code is the way it is** —
  constraints, incidents, tuning rationale — record it with
  `memway_meta` on that entity before you reply. This is due whenever a
  reason SURFACES, not only when a change lands; work you decline or
  block on counts too. The reason a change was refused is often the
  most valuable thing to record.

## 2. Project lessons

Method knowledge, coordinate-free by nature. These do not attach to any
entity, so they live here (finding #49).

**1. Falsifications must receipt their sabotage.** A falsification run
only counts if the sabotage provably applied: `assert old in s` before
the write, print `[sabotage applied]` after. Three sessions were lost to
`write_text(s.replace(old, new), 1)` — the `1` lands as `encoding`,
`write_text` throws, the sabotage never applies, and eight green
falsifications proved nothing. A test of a test can be vacuous too.

**2. Verify state, not signals.** Success messages, exit codes, and
rendered output are claims, not facts. `gh` printed help and changed
nothing; `sed` exited 0 against pretty-printed JSON; a demo rendered
beautifully with a dead filter. Read the artifact back.

**3. Reads must not write.** The read fence
(`tests/test_read_fence.py`, 7/7 surfaces) is load-bearing. Three
"read" paths turned out to write, and nobody noticed until something
fingerprinted state before and after. A new read surface goes under
`read_only()` and into that test.

**4. Derived splits three ways.** Authored is precious (`meta/` — never
bulk-deleted). Regenerable derives (caches, `evidence/`) may be skipped
or cleared. SNAPSHOT BASELINES (`docbindings.json`, `versions/`) are
neither: they are the ruler drift is measured against, and suppressing
their writes makes staleness read permanently fresh.

**5. Fixtures encode what you thought of; corpora contain what you
didn't.** G4 shipped broken twice past hand-built unit tests and died in
minutes against real repositories. Big changes get a real-repo run
before they get believed. A wrong fixture can convict correct code, so
verify the fixture before the verdict: a doc-comment fixture that put the
comment above the wrong declaration nearly had the README declare a
working parser broken.

**6. Falsify against committed state, restore from a copy.**
`git checkout --` during falsification silently reverted uncommitted
work twice, and nearly reported a live guard as dead code.

**7. Stamping and reading have one implementation each.** `stamp_for`
and `accepted_for` in `memway/metadata.py` are the only write-side and
read-side hash rules; a test asserts no module reimplements them. Three
surfaces had drifted apart before this existed — the same note decayed
at different rates depending on which one wrote it.

**10. Turn on your own automation.** memway shipped hooks that keep the
map in step with the tree, and its OWN repo had none of them installed -
found on 2026-08-16 when the map was two commits behind while the
rendered page had just been republished from it. Every stale map this
session traces to that: the index was updated by hand, by memory, at the
end of long sessions. `memway hooks install .` is now done here. A tool
whose flagship repo does not use its own automation is reporting on a
configuration nobody actually runs.

**9. The release ceremony must ask what it invalidated.** The workflow
rules tell an agent to supersede staled knowledge before finishing, and
0.54.1 shipped that rule and then broke it within the hour: nine modules
changed, six coordinates went coral, and the flagship map spent a day
advertising two defects that the same release had fixed. It happened
twice in one evening. That is not discipline failing twice, it is a
checklist missing a line - nothing between "edits done" and "commit"
ever asked. So the close now runs, before the commit:

    memway --json verify-change .    # staled_knowledge AND
                                     # rotted_comments must be empty
    memway index .                             # then, and only then:
    memway viz . --out docs/map.html --force   # the shop window
    pytest -m release                          # and the gate that proves it

ORDER MATTERS HERE TOO, in the opposite direction to verify-change. The
notes you just wrote are stamped against the WORKING TREE, so they read
stale until the index catches up - render before indexing and you publish
a page announcing staleness you created and already answered. Caught
immediately: the first run of this very step rendered "5 stale" for the
five confirms written moments earlier.

BOTH LISTS, since 0.55.4. A change invalidates two things: the knowledge
attached to the map, and the comments sitting in the source. The second
was detected all along and only ever reported to a queue somebody had to
remember to visit, so 49 rots accumulated on this repo while every
release closed clean. Same gate, same moment, and never blocking: a rot
you cannot fix now takes a `confirm` ("read it, the logic moved, the
comment is still accurate"), which clears it honestly by suppression
until the logic moves again. Accepting it sends it to `attention`. What
the ceremony refuses is walking past it in silence.

THE SHOP WINDOW IS DERIVED AND NOTHING ELSE KEEPS IT HONEST. `.coord`
rides every commit through the pre-commit hook; `docs/map.html` does not,
and it drifted NINE releases before a human noticed - serving a
superseded note badged fresh, against a hash that had moved. Regenerate
it in the close and let `pytest -m release` prove it, rather than
trusting that somebody remembered.

If staled_knowledge is not empty, supersede in the SAME channel first - a
confirm does not answer a stale note. Detection without a prompt at the right moment
closes nothing, which is the argument the release itself made.

RUN IT BEFORE `memway index`, NOT AFTER. verify_change compares the
working tree against the STORED index, so re-indexing first makes the
two agree and it reports `changed: 0, staled_knowledge: 0` on a release
that staled plenty - a green light generated by the act of checking.
Caught in 0.55.2, on this checklist, by the agent following it: the
honest run named four coordinates. If the index has already been
refreshed, ask `memway attention` or the ambient `knowledge_lag` instead;
those read the stamps and do not care what order you did things in.

**8. Irreversible public actions get human hands.** PyPI uploads,
public repo creation, force-pushes: prepare everything, verify
everything, then hand over the final command.

**12. A signal must have a boundary, or it is not a signal.** Comment
rot asks "did the comments stay while the logic moved" - exact for a
function, whose comments and body share one scope. It was applied to
MODULES for three releases and could never be right there: this repo's
own `indexer.py` docstring claims things about its module surface, about
behaviour inside its functions, AND about `lineage.py` - three scopes in
one paragraph, and no hash of that file bounds them. Hashing the whole
file re-flagged every module on every edit, so a confirm could never
stick and 14 permanent coral entries accumulated. Hashing only the
module's surface was the planned fix and was worse where it counts:
clearable, but silently blind to a docstring describing behaviour a
function body implements - confident and incomplete, which is the failure
mode this project treats as most serious. So memway stops asking. Module
docstring review is a deliberate human task. Before adding a check, ask
what bounds the thing it claims to check; if nothing does, the honest
build is no check rather than an approximate one wearing a precise name.

**11. Any string containing a version, a name, or a command list must
derive from the value it describes** - prose constants rot on every
surface we have shipped them. Five specimens, all found by a human
reading a screen rather than by any test: the browser tab that called
every map `itsdangerous`; the wordmark that did not match the site; the
`hooks install` banner describing commands the hooks no longer ran; the
migration message announcing `(v1 -> v2)` while `SKETCH_VERSION` was 3;
and `memway freshness` answering a typo with the whole manual instead of
the word it did not understand. The pattern is not carelessness - every
one of these sat beside code that was correct, under tests that all
passed, because **a constant describing behaviour is invisible to a test
that checks behaviour**. So derive it, and pin the derivation: assert the
value comes from the source (AST or execution), not that the sentence
reads right today.

**13. Knowledge written through a shell is knowledge the shell may
rewrite.** A confirm was passed to `memway meta` inside a double-quoted
zsh string containing backticks; zsh ran them as command substitution and
stored the sentence with two words silently deleted. The CLI printed
"added confirm entry" and exited 0 - correctly, because it faithfully
stored what it was handed. This is lesson 2 in a new place: the success
line describes the WRITE, not the CONTENT. Prose entries are the one
artifact in this project nobody can reconstruct, so write them through
the API or from a file rather than an interpolated command line, and read
the entry back before believing it landed.

## 3. Where knowledge goes

- **A fact about an entity** → `memway_meta` (stamped, staleness-audited).
- **A method or project lesson** → this file, section 2.
- **Strategy or roadmap** → memway-tasks (private).

If unsure: is this about an entity, or about how we work?
