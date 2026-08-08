"""The parse cache is keyed on FILE CONTENT, not on parser version.

So when a parser starts emitting something new, every existing index
keeps replaying the old cached entities and the change is invisible -
silently, and for as long as the source files themselves stay untouched.
PARSE_SCHEMA_VERSION exists to force that cache to be discarded, but
bumping it was a manual step with nothing enforcing it.

That failed in practice during the go/js/ts signature work: parsers.py
was edited, the repo reindexed, and .ts entities kept empty signatures
until the cache was deleted by hand.

This test pins parsers.py to the schema version it was released under.
Editing the parsers fails it, which is the point: the fix is a decision,
not a chore.
"""

import hashlib
from pathlib import Path

from memway.parsers import PARSE_SCHEMA_VERSION

# sha256 of memway/parsers.py as released under the version below.
_PARSERS_SHA = "9a85df806ce4422cce49d7597810a8a3cdb2dd39e1d3fd08403b1887a2c4e5e5"
_SCHEMA_AT_SHA = 3

_GUIDANCE = """
memway/parsers.py has changed since PARSE_SCHEMA_VERSION was last pinned.

Decide which kind of change this is:

  * It changes parser OUTPUT (new/changed RawEntity or RawEdge fields,
    different qualnames, extra entities, altered signatures/docs) ->
    bump PARSE_SCHEMA_VERSION in memway/parsers.py, THEN update both
    _PARSERS_SHA and _SCHEMA_AT_SHA in this test. Without the bump,
    every existing index serves stale cached entities indefinitely.

  * It is cosmetic (comments, docstrings, formatting, refactoring with
    identical output) -> update _PARSERS_SHA only, leaving
    _SCHEMA_AT_SHA as it is.

Recompute with:
    python3 -c "import hashlib,pathlib; \
print(hashlib.sha256(pathlib.Path('memway/parsers.py').read_bytes()).hexdigest())"
"""


def test_parser_change_requires_a_schema_decision():
    src = Path(__file__).resolve().parent.parent / "memway" / "parsers.py"
    digest = hashlib.sha256(src.read_bytes()).hexdigest()

    assert digest == _PARSERS_SHA, _GUIDANCE
    assert PARSE_SCHEMA_VERSION == _SCHEMA_AT_SHA, (
        f"PARSE_SCHEMA_VERSION is {PARSE_SCHEMA_VERSION} but parsers.py is "
        f"unchanged at the source pinned to version {_SCHEMA_AT_SHA}. "
        "Update _SCHEMA_AT_SHA in this test to match the new version."
    )


def test_indexer_reads_the_version_it_writes():
    """The bump only helps if both sides of the cache use the constant."""
    indexer_src = (Path(__file__).resolve().parent.parent
                   / "memway" / "indexer.py").read_text()
    assert indexer_src.count("PARSE_SCHEMA_VERSION") >= 3, (
        "indexer.py must compare the cached _schema against "
        "PARSE_SCHEMA_VERSION on read and stamp it on write; a cache that "
        "is written without the marker never invalidates."
    )
