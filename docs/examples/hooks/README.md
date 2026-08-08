# Optional: the capture hook

`memway setup` **never installs this.** It is an example you can wire up
yourself if you want the capture rule enforced rather than requested.

## What it enforces

The third workflow rule asks an agent to record the reason behind a
change. In practice that rule is followed reliably when the agent is
already in a tool-using loop — edit, verify, capture — and skipped when a
session ends by answering you. An investigation or a refusal has no
end-of-work moment for the rule to attach to, and those are exactly the
sessions whose reasons are most worth keeping.

This hook supplies the moment. On `Stop` it checks whether `memway_meta`
was called; if the session used tools and recorded nothing, it blocks
once and says so. It also reports `comment_rot` on entities the session
itself edited — a flag `before_edit` cannot warn you about, because it
runs before the edit exists, and `verify_change` does not report at all.

## What it does not do

It stays silent when:

- the session used no tools (a conversation, not work)
- `memway_meta` was actually called
- it already blocked once this turn — it cannot loop
- the transcript is missing or unreadable
- a rotted entity already carries a `confirm` at its current hash

That last one matters. An earlier version skipped it and its first real
firing demanded confirms on seven entities that already had them. A gate
that re-asks for completed work is how a check becomes noise people learn
to click through.

## The escape hatch is load-bearing

The block text explicitly permits *"nothing durable surfaced — say so in
one line and stop."* Keep it. Without a legitimate way out, a gate
manufactures compliance: agents write filler to clear the alert, and
filler is indistinguishable from signal when you read the map later.
That is worse than the missing entry it was built to prevent.

## Wiring it

Copy the script somewhere in your project and point a `Stop` hook at it:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ {
          "type": "command",
          "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/capture-guard.py\"",
          "timeout": 15,
          "statusMessage": "Checking knowledge capture..."
      } ] }
    ]
  }
}
```

Requires Python 3 on `PATH` — no third-party packages, no `jq`.

To remove it, delete the `Stop` entry. Nothing else depends on it.

## Debugging

Each run appends one line to `/tmp/memway-hook.log`:

```
ran tools=True called_meta=False rot=0 blocking=True
```

A hook that stays silent and a hook that was never configured look
identical from the outside. The log tells them apart.
