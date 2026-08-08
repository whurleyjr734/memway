# Contributing to memway

Thanks for being here. Bug reports, small fixes, and hard questions about
the design are all welcome.

## Before a large change, open an issue

For anything beyond a small fix, open an issue first and let's talk about
it. This is a young project with strong opinions about what belongs in
it, and a conversation costs ten minutes where a declined pull request
costs you a weekend.

Small, obvious fixes — a typo, a broken edge case, a missing test — just
send them.

## The CLA

Contributions require agreeing to the
[Contributor License Agreement](CLA.md). It is one page and written to be
read.

**Why:** memway is AGPL-3.0, and the CLA keeps future licensing options
open — including selling commercial licenses to organizations that cannot
use AGPL code, which is how the project can pay for itself. That only
works if the copyright stays consolidated, which is what the CLA does.
You keep the copyright in your own contribution; you grant a license
broad enough that the project can be offered under other terms too.

**How to agree:** put this line in your pull request description:

> I have read and agree to the CLA.

That's it for now. If the project grows enough to need automation, we'll
add a bot and say so.

## Pull requests

- **Tests are required** for behavior changes. A bug fix should come with
  the test that would have caught it.
- **`python3 -m pytest -q` must be green** — bare, no flags, no skips. If
  a test is slow or environment-dependent, that's worth discussing in the
  issue rather than marking it skipped.
- **One logical change per pull request.** Two good changes in one PR are
  harder to review, harder to revert, and harder to explain in a commit
  message than two PRs.
- **Update the map.** If your change adds, removes, or renames entities,
  run `memway index .` and include the resulting `.coord/` changes. The
  map is committed on purpose — it travels with the repo, so a clone
  arrives with the project's memory intact.
- **Editing `memway/parsers.py`?** Parser output is cached by file
  content, so a change that alters what the parsers *emit* needs
  `PARSE_SCHEMA_VERSION` bumped or every existing index silently replays
  stale entities. A test enforces this and will tell you which case
  you're in.

## Commit messages

Say what changed and why it changed. The "why" is the part that can't be
recovered from the diff later — that's the whole thesis of this project,
so it would be strange not to practice it here.

## Running things locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,languages]"
python3 -m pytest -q
```

Python 3.10+. The core has zero runtime dependencies; the `languages`
extra pulls tree-sitter grammars for Go, Java, JavaScript and TypeScript.

## Conduct

Be decent. Assume the other person is smart and busy.
