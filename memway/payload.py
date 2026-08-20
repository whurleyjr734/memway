"""One truncation rule, for every payload this project returns.

WHY THIS IS A MODULE AND NOT A LINE OF CODE. These payloads are read by
agents, so an unbounded list is not clutter you skip - it is a context
budget you cannot get back. `before_edit` on a hot entity in prometheus
returned 53,534 characters, and `show` on the same entity returned
55,829, of which 55,572 was a single 344-entry `edges` list.

Truncation was already happening in five places, each written by hand and
each slightly different: `attention` capped markers at 20 and reported a
total, `attention` capped comment rot at 20 and reported a differently
named total, `summary` sliced knowledge entries at 20 and reported
nothing, `summary` sliced two hot-spot lists at 5 and reported nothing,
and `before_edit` had just grown a sixth. Five copies of one rule is how
a rule gets fixed in one place and not the others - the same shape this
codebase already pinned against for the stamping rules and the ring rule.

THE RULE HAS THREE PARTS AND ALL THREE ARE REQUIRED.

  RANK    the useful entries first, because a bounded list is only as
          good as its ordering. Truncating an arbitrary order throws
          away the answer as often as the noise.
  BOUND   at CAP, so one call cannot swallow a budget.
  REPORT  <name>_total and <name>_shown, always. A list that quietly
          stops at twelve IS a sampled list, and the guard message this
          project ships elsewhere reads "Nothing is ever sampled
          silently". Two of the five sites above were silent; a reader
          had no way to know a slice had happened at all.

The reporting keys are derived from the list's own name so a payload can
never disagree with itself about what was cut.
"""
from __future__ import annotations

# Twelve. Enough to see the shape of a neighbourhood, small enough that a
# dozen of these in one session is still affordable.
CAP = 12


def rank_bound_report(items, name: str, *, rank=None, cap: int = CAP):
    """Return (shown, report) - the ONLY way a payload truncates a list.

    `rank` is a sort key applied before the cut; omit it only when the
    incoming order is already meaningful. `report` is a dict of
    `<name>_total` and `<name>_shown`, meant to be splatted into the
    payload beside the list itself.
    """
    seq = list(items)
    total = len(seq)
    if rank is not None:
        seq.sort(key=rank)
    shown = seq[:cap] if cap is not None else seq
    return shown, {f"{name}_total": total, f"{name}_shown": len(shown)}
