"""How an entity reference is spelled. ONE producer, every speller.

THE DEFECT THIS EXISTS TO END. A qualname can carry a disambiguator -
Java overloads register as `Type.method/2`, and same-named Python defs
register as `name#3` - and until 0.56.0 those two suffixes were produced
in two different layers by two pieces of code that had never heard of
each other:

    parsers.py:670   q = f"{owner}.{name}/{a}"      the Java PARSER
    indexer.py:747   qualname = f"{qualname}#{occ}" the REGISTRY

Meanwhile every call target was emitted as a BARE name. So the registry
stored one spelling and the emitter wrote another, and resolution
compared the wrong ends. Measured on google/gson: 3,235 of 3,235 method
qualnames carry `/`, 0 of 22,453 emitted references do, and 0 of 1,770
resolved call edges reach a method - every Java method reporting
"callers 0, blast 0". The same shape costs Python the @overload case,
where a call lands on the first stub while the helper it should reach
hangs under `iter_content#3`.

Neither suffix is wrong. Both disambiguate real ambiguity. The defect is
that only one side of the conversation knew the convention, and nothing
asserted they still agreed - which is why this module is a producer and
not a formatting helper. A future disambiguator added here reaches the
emitter, the registry and the resolver at once, or it does not exist.

Related lesson, same root: CLAUDE.md 11 (derive, don't restate).
"""
from __future__ import annotations

import re

# The characters that introduce a disambiguator. Kept in ONE place so
# base_of, arity_of and every caller agree on what a suffix looks like.
ARITY = "/"
OCCURRENCE = "#"
_SUFFIX = re.compile(rf"[{re.escape(ARITY)}{re.escape(OCCURRENCE)}].*$")


def render(base: str, *, arity: int | None = None,
           occurrence: int | None = None) -> str:
    """The spelling of a reference. The ONE place a suffix is attached.

    `arity` is the parameter count for languages that disambiguate
    overloads by signature (Java). `occurrence` is the nth same-named
    definition in one scope (Python). They are not combined today; if a
    language ever needs both, it is added here and every speller gets it.
    """
    out = base
    if arity is not None:
        out = f"{out}{ARITY}{arity}"
    if occurrence is not None:
        out = f"{out}{OCCURRENCE}{occurrence}"
    return out


def base_of(ref: str) -> str:
    """A reference with any disambiguator removed, LAST SEGMENT ONLY.

    `a.b.method/2` -> `a.b.method`, `m.f#3` -> `m.f`, `m.f` -> `m.f`.
    Only the final segment is stripped: a package path may legitimately
    contain neither character, and stripping earlier ones would maul
    qualnames rather than normalise them.
    """
    if not ref:
        return ref
    head, _, last = str(ref).rpartition(".")
    stripped = _SUFFIX.sub("", last)
    return f"{head}.{stripped}" if head else stripped


def short_of(ref: str) -> str:
    """Just the final name, no path and no disambiguator."""
    return base_of(ref).split(".")[-1]


def arity_of(ref: str) -> int | None:
    """The arity a reference declares, or None if it declares none.

    This is what lets the resolver break an overload tie with the
    argument count at the call site instead of guessing.
    """
    last = str(ref).split(".")[-1]
    i = last.find(ARITY)
    if i < 0:
        return None
    tail = last[i + 1:].split(OCCURRENCE)[0]
    return int(tail) if tail.isdigit() else None
