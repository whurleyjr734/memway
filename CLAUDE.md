# Project rules

This repo IS memway, and it uses itself. The map in `.coord/` is
committed and live (635 entities, 45 knowledge entries) — it is not a
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

    memway --json verify-change .    # staled_knowledge must be empty

If it is not empty, supersede in the SAME channel first - a confirm does
not answer a stale note. Detection without a prompt at the right moment
closes nothing, which is the argument the release itself made.

**8. Irreversible public actions get human hands.** PyPI uploads,
public repo creation, force-pushes: prepare everything, verify
everything, then hand over the final command.

## 3. Where knowledge goes

- **A fact about an entity** → `memway_meta` (stamped, staleness-audited).
- **A method or project lesson** → this file, section 2.
- **Strategy or roadmap** → memway-tasks (private).

If unsure: is this about an entity, or about how we work?
