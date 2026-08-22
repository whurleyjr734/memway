"""
Per-coordinate metadata store.

Each coordinate ID owns a folder under .coord/meta/<coord_id>/ containing
typed metadata channels. Knowledge attaches to the stable ID, not to a
file path, so it survives renames and refactors (via lineage).

Channels:
  history  - what changed and when (append-only)
  design   - why it is built this way
  notes    - gotchas, invariants, warnings
  docs     - documentation
  traces   - recorded data-flow traversals (structural stack traces)
  confirm  - attestation that current comments/docs remain accurate after logic change

Entries come in two kinds, and the difference is not the channel:

  a CLAIM   says something, and takes a position in the reading order -
            the newest claim in a channel supersedes the ones behind it.
  a STAMP   says nothing and supersedes nothing. It re-dates the claims
            already there ("I read these, they still hold"), and carries
            `reaffirms: <n>`. See MetaStore.reaffirm for why saying
            nothing needed its own shape rather than another paragraph.
"""

import json
import time
from pathlib import Path

CHANNELS = ("history", "design", "notes", "docs", "traces", "confirm")


class GhostEntity(LookupError):
    """The ref resolved in the stored map but not in the working tree."""


def stamp_for(entity, repo_root=None) -> str:
    """The hash a new entry is stamped with. THE one write-side rule.

    LOGIC HASH FIRST. A note describes behaviour, so it should survive a
    reformat, a comment fix or a docstring tweak, and expire when the
    behaviour actually moves. Falls back to body_hash for entities whose
    language has no logic tier (see indexer._logic_hash's honest
    degradation).

    STAMPED AGAINST THE WORKING TREE, not the stored index, whenever
    repo_root is given. The entity handed in came from the map, which is
    by definition a snapshot of the last index - so stamping from it
    produced notes that were BORN STALE. Measured end to end: edit a
    function, let the pre-commit hook name the note it staled, supersede
    exactly as instructed, re-index, and the brand-new note reads
    stale=True. The release's whole promise is "you will be told, then
    supersede"; superseding as told healed nothing unless you happened to
    re-index first, which nothing tells you to do.

    So this re-derives the hash from the current source. Reads files, does
    not write: the write scope of a meta call is still exactly one file,
    the note itself, and a test asserts that.

    Raises GhostEntity when the ref no longer resolves in the working
    tree - deleted or renamed since the last index. Stamping against a
    ghost would mint a note that can never be fresh and can never be
    superseded, attached to a coordinate the code has abandoned. Refuse
    and say so; never guess.

    This exists because it DRIFTED. `memway meta` stamped body_hash while
    the MCP `agent_meta` stamped logic_hash, so the same note written
    from two surfaces decayed at different rates - a docstring edit
    staled the CLI's copy and left the agent's fresh. query.py is the
    single core for reads for exactly this reason; this is that principle
    extended to writes. Every write path calls this. Do not inline it.
    """
    if repo_root is not None:
        fresh = _current_entity(entity, repo_root)
        if fresh is None:
            raise GhostEntity(
                f"{entity.qualname!r} is in the map but not in the working "
                f"tree - deleted or renamed since the last index. Re-index "
                f"(memway index .) and attach the note to the coordinate "
                f"that exists, rather than stamping one that never can be "
                f"fresh.")
        entity = fresh
    return getattr(entity, "logic_hash", "") or entity.body_hash


def _current_entity(entity, repo_root):
    """The same entity as the working tree has it, or None if it is gone.

    Uses the in-memory index path added in 0.54.1 - index(persist=False)
    computes every hash and writes nothing, including no parse-cache
    refresh. Reusing it rather than re-parsing one file by hand keeps ONE
    implementation of what a logic hash is; a second parse path here is
    exactly how stamp_for and agent_meta drifted apart the first time.
    """
    try:
        from .indexer import Indexer
        repo_root = Path(repo_root)
        ix = Indexer(repo_root, repo_root / ".coord")
        ix.load_existing(write_cache=False)
        ix.index(persist=False)
        cid = ix.by_qualname.get(entity.qualname)
        return ix.entities.get(cid) if cid else None
    except Exception:
        return entity          # a broken re-parse must not block a write


def accepted_for(entity) -> set:
    """Hashes that count as CURRENT when reading. The read-side twin.

    Both tiers are accepted so that entries stamped before `stamp_for`
    existed - body-stamped - stay valid until their text actually
    changes, rather than being retroactively invalidated.
    """
    return {getattr(entity, "logic_hash", ""), entity.body_hash} - {""}


def for_display(md: dict) -> list:
    """Channel entries flattened for a HUMAN to read: newest first.

    THE READING ORDER, and it is not the file order. Entries are
    append-only, so the file runs oldest -> newest; rendering it straight
    through put the entry the ring rule DISCARDED at the top and the one
    that decides at the bottom. On a coordinate whose ring says fresh, the
    first thing a reader saw was a note marked STALE describing a bug that
    no longer existed. The ring and the panel disagreed on screen.

    The tell that this is the right way round: six superseding notes
    written on this repo said "supersedes the note BELOW it" - true of the
    file, false of the screen, until this. Newest first makes the sentence
    true, because what you superseded is now genuinely underneath.

    SUPERSEDED IS NOT STALE. Stale means "the code moved under this and
    nobody has answered"; superseded means "somebody answered, and this is
    the older answer". The first is a warning, the second is history, and
    a panel that renders them identically teaches people to ignore both.
    Only the newest entry per channel can be a warning; everything behind
    it is marked superseded regardless of its own stale flag.

    REAFFIRMATIONS ARE NOT ENTRIES AND DO NOT SUPERSEDE. A reaffirmation
    carries a stamp and no claim (see MetaStore.reaffirm). Letting one take
    a position here would be the worst of both worlds: it would render as
    an empty note at the top of the panel, and - because supersession is
    positional - it would mark the substantive note it was vouching FOR as
    superseded history. The entry it re-stamps is exactly the entry a
    reader wants to see, so it stays newest and picks up `reaffirmed_ts`
    and `reaffirmed_by` from MetaStore.read.
    """
    out = []
    for channel, entries in md.items():
        rows = [en for en in entries if not en.get("reaffirms")]
        for i, en in enumerate(reversed(rows)):
            out.append({**en, "channel": channel, "superseded": i > 0})
    return out


def rot_is_answered(md: dict) -> bool:
    """Does a CURRENT confirm answer this entity's comment rot?

    THE ONE SUPPRESSION RULE. Comment rot says "the logic moved and the
    comments did not"; a confirm says "I read them, they still describe
    this". The confirm is hash-stamped like any entry, so it answers only
    until the logic moves again - which is what makes this honest rather
    than a mute button.

    Pass the output of MetaStore.read_all(cid, accepted_for(entity)); the
    staleness flags must already be applied or every old confirm counts.

    Extracted in 0.55.4 because it was about to have a second copy. This
    rule lived inline in attention, and verify_change needed exactly the
    same test to report rot at commit time. Every defect this project
    found in the preceding four releases had that shape: the ring rule
    hand-rolled in the queue, the .coord exclusion missed by a second
    commit count, one metadata import written four ways. A rule with two
    call sites gets one implementation, before the copies drift and not
    after.
    """
    return any(not en.get("stale") for en in md.get("confirm", []))


def unsuperseded_stale(knowledge: list) -> list:
    """THE ONE RING RULE, as rows: the entries that are stale AND decisive.

    Entries are append-only and never deleted, so a coordinate accumulates
    its own history: re-reading the code and writing a fresh entry leaves
    the superseded one on disk forever. A rule of "any entry is stale"
    means a coordinate that went coral once stays coral no matter how many
    times somebody answers it - measured on memway's own map, 12 rings
    that no amount of fresh confirmation could clear.

    ORDER-INDEPENDENT BY CONSTRUCTION. This took the LAST row per channel
    until 0.54.2, which was correct only while every caller passed file
    order - and 0.54.2 made the human-facing order newest-FIRST. A
    positional rule would have inverted silently, calling a coordinate
    fresh on the strength of a note somebody had already replaced. Flags
    survive reordering; positions do not.

    Callers pass for_display() output. A row with no flag counts as
    decisive, which is the safe direction: it can raise a warning a human
    dismisses, never suppress one they needed.

    Two shapes, ONE implementation: viz.has_unsuperseded_stale asks
    whether to draw a ring, verify_change asks WHICH entries a change
    invalidated, _knowledge_lag asks how many coordinates are affected. A
    second copy is how the behind-count shipped without the exclusion the
    dirty check already had.
    """
    return [r for r in knowledge
            if r.get("stale") and not r.get("superseded")]


class MetaStore:
    def __init__(self, coord_dir: str):
        self.root = Path(coord_dir) / "meta"

    def _channel_file(self, coord_id: str, channel: str) -> Path:
        if channel not in CHANNELS:
            raise ValueError(f"unknown channel {channel!r}; use one of {CHANNELS}")
        d = self.root / coord_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{channel}.jsonl"

    def add(self, coord_id: str, channel: str, text: str,
            author: str = "human", body_hash: str = "", **extra):
        """Append an entry to a coordinate's metadata channel.
        D10: entries are stamped with the entity's body_hash at write
        time; when the code later changes, the stamp mismatch marks the
        entry stale (trust decays, entries never silently vanish)."""
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "author": author, "text": text}
        if body_hash:
            entry["body_hash"] = body_hash
        entry.update(extra)
        f = self._channel_file(coord_id, channel)
        with f.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def read(self, coord_id: str, channel: str,
             current_hash="") -> list[dict]:
        f = self.root / coord_id / f"{channel}.jsonl"
        if not f.exists():
            return []
        out = [json.loads(line) for line in f.read_text().splitlines()
               if line]
        if current_hash:
            # current_hash may be a single hash or an iterable of accepted
            # hashes (e.g. {logic_hash, body_hash}); a note is fresh if its
            # stamp matches ANY accepted hash. Lets logic-stamped notes
            # survive cosmetic edits, and old body-stamped notes stay valid
            # until the text actually changes.
            accepted = ({current_hash} if isinstance(current_hash, str)
                        else set(current_hash))
            accepted.discard("")
            for e in out:
                if e.get("body_hash") and e["body_hash"] not in accepted:
                    e["stale"] = True
            # A REAFFIRMATION RE-STAMPS THE ENTRIES BEHIND IT. "I read
            # these at hash H and they still hold" is a stamp, not a
            # claim, so it updates their acceptance rather than adding
            # prose that repeats them (MetaStore.reaffirm has the why).
            # The LAST accepted reaffirmation wins, and it vouches only
            # for entries written BEFORE it - nothing can attest to text
            # that did not exist when it was written.
            # `default=-1` would make the slice below out[:-1] and clear
            # staleness on every entry but the last, so the absence of a
            # reaffirmation is checked explicitly rather than encoded as
            # an index.
            stamps = [i for i, e in enumerate(out)
                      if e.get("reaffirms") and e.get("body_hash") in accepted]
            if stamps:
                last = stamps[-1]
                for e in out[:last]:
                    e.pop("stale", None)
                    e["reaffirmed_ts"] = out[last].get("ts", "")
                    e["reaffirmed_by"] = out[last].get("author", "")
        return out

    def reaffirm(self, coord_id: str, channel: str, author: str = "human",
                 body_hash: str = "") -> dict:
        """Re-stamp a channel's existing entries at the current hash.

        WHY THIS IS NOT JUST ANOTHER `add`. Measured on memway's own map
        at 0.60.1: of 257 knowledge entries, 176 (68%) were confirms -
        attestation rather than knowledge - carrying 14,186 words that
        almost entirely repeated the entry beneath them. The cause is
        structural, not a discipline failure. Answering a staled entry
        means supersede or confirm, so an entry that is STILL TRUE can
        only be cleared by WRITING A PARAGRAPH SAYING IT IS STILL TRUE.
        Every false stale manufactures prose, and because a confirm is
        itself a hash-stamped entry, it stales in turn and demands a
        confirm on the confirm - seven identical ones accumulated on
        memway.__init__ that way before the generator was removed.

        So the affirmation becomes what it always was: a stamp. Same
        append-only file, same hash rule, no prose required.

        THE FIRST ATTESTATION IN A CHANNEL MUST STILL BE PROSE, and this
        refuses when there is nothing to re-stamp. That line is the whole
        safety of the feature. A reaffirmation with an empty channel
        behind it would assert nothing while clearing a warning, which is
        a mute button wearing the word `confirm` - exactly the confirm
        fatigue this is meant to end, arrived at from the other side.
        Somebody has to say the thing once; only the repeats are free.

        THE REMEDY IS THE DOOR'S TO PHRASE, NOT THIS METHOD'S. The first
        version of this message ended "write it with `meta --channel
        confirm`" and that command does not exist: `meta` takes the
        channel POSITIONALLY, and `--channel` is `search`'s spelling. So
        the refusal handed the reader a line that fails. Worse, an MCP
        caller has no CLI at all and was being told to run one.

        Found by exercising the feature, not by a test - the tests
        asserted the message was PRESENT, which a wrong message satisfies
        exactly as well as a right one (lesson 11: a constant describing
        behaviour is invisible to a test that checks behaviour). Both
        doors now append their own remedy and both are pinned by
        EXECUTING it.

        A RE-STAMP OF SOMETHING ALREADY CURRENT IS THE ORIGINAL DISEASE.
        Returns None and writes NOTHING when every claim in the channel is
        already accepted at this hash. Without that, five `affirm` calls
        on an unchanged coordinate left one claim and five stamps - each
        recording that a hash which never moved still had not moved. An
        agent looping this over a repo would grow the store forever, which
        is the volume problem this method exists to end, rebuilt in a
        cheaper format. Measured, not theorised: it is exactly what
        happened the first time the tool was pointed at a fresh channel.

        So the write is conditional on there being something stale to
        answer, and "nothing needed" is a success, not an error - a script
        sweeping coordinates must not fail on the ones that were fine.

        Raises ValueError stating what is missing; callers add the how.
        """
        prior = [e for e in self.read(coord_id, channel)
                 if not e.get("reaffirms")]
        if not prior:
            raise ValueError(
                f"nothing to reaffirm: {coord_id} has no {channel} entry "
                f"to re-stamp - the first attestation has to say "
                f"something, and only the repeats are free")
        if body_hash:
            current = self.read(coord_id, channel, current_hash=body_hash)
            if not any(e.get("stale") for e in current
                       if not e.get("reaffirms")):
                return None
        return self.add(coord_id, channel, "", author=author,
                        body_hash=body_hash, reaffirms=len(prior))

    def read_all(self, coord_id: str, current_hash="") -> dict:
        return {ch: entries for ch in CHANNELS
                if (entries := self.read(coord_id, ch, current_hash))}

    def migrate(self, old_id: str, new_id: str, note: str = "",
                new_body_hash: str = ""):
        """Move metadata from one coordinate to another (used by lineage
        when an entity is renamed/split and knowledge must follow)."""
        old_dir = self.root / old_id
        if not old_dir.exists():
            return
        new_dir = self.root / new_id
        new_dir.mkdir(parents=True, exist_ok=True)
        for f in old_dir.glob("*.jsonl"):
            target = new_dir / f.name
            lines = f.read_text().splitlines()
            with target.open("a") as out:
                for line in lines:
                    if not line:
                        continue
                    # detected renames preserve semantics (shape match),
                    # so migrated entries are re-stamped to the new hash
                    # rather than falsely flagged stale
                    if new_body_hash:
                        e = json.loads(line)
                        if e.get("body_hash"):
                            e["body_hash"] = new_body_hash
                        line = json.dumps(e)
                    out.write(line + "\n")
        self.add(new_id, "history",
                 f"metadata inherited from {old_id}. {note}".strip(),
                 author="lineage")


class TraceRecorder:
    """Records data-flow traversals as sequences of coordinates.

    This is the 'structural stack trace': instead of log lines (the
    author's guesses about what matters), we record where data actually
    went, coordinate by coordinate, with optional payload notes.
    """

    def __init__(self, meta: MetaStore, trace_id: str):
        self.meta = meta
        self.trace_id = trace_id
        self.hops: list[dict] = []

    def hop(self, coord_id: str, note: str = "", payload_summary: str = ""):
        self.hops.append({
            "seq": len(self.hops),
            "coord": coord_id,
            "note": note,
            "payload": payload_summary,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        return self

    def commit(self):
        """Write the full trace to every coordinate it touched."""
        path = [h["coord"] for h in self.hops]
        for h in self.hops:
            self.meta.add(h["coord"], "traces",
                          h["note"] or "(hop)",
                          author="trace",
                          trace_id=self.trace_id,
                          seq=h["seq"],
                          full_path=path,
                          payload=h["payload"])
        return path
