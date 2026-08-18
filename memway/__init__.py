"""memway - a map of your codebase: coordinates, flow, and memory."""


def _derive_version() -> str:
    """The version, DERIVED from whatever actually defines it here.

    CLAUDE.md lesson 11: a constant describing behaviour is invisible to a
    test that checks behaviour. This module used to restate the version as
    a literal, and pinning it to pyproject.toml only proved the two strings
    matched - it did nothing about the two costs the literal imposed.

    COST ONE, the flake. Python's timestamp .pyc invalidation compares
    source mtime and size at one-second granularity, and a version bump
    changes neither: "0.55.3" and "0.55.4" are the same length and the edit
    lands in the same second as the preceding build's .pyc. The cache then
    stays valid forever while serving the OLD string. It fired on four
    consecutive releases. Reading the file at import removes the constant
    that a stale cache could hold: old bytecode running this function still
    computes today's answer.

    COST TWO, and the reason this finally got fixed. The literal changed on
    every single release, which moved the module's logic hash, which staled
    every note attached to this coordinate. Seven consecutive releases each
    answered that with a hand-written confirm saying the comments were
    still accurate. That is a tool generating work for itself.

    ORDER. A source tree or editable install is defined by its
    pyproject.toml, which sits beside the package - so it wins, and an
    editable checkout can never report the version it was installed at.
    A wheel has no pyproject.toml next to it, so the distribution metadata
    answers, which is correct: for a wheel, metadata IS what was installed.
    cli._version still owns the editable-vs-wheel decision; this only
    decides what "the source tree says" means.
    """
    from pathlib import Path

    pp = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        text = pp.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if text:
        # Scoped to [project], because `version = ` appears under other
        # tables too and a bare search would take whichever came first.
        section, name, found = None, None, None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                section = s[1:-1]
                continue
            if section != "project" or "=" not in s:
                continue
            key, _, val = s.partition("=")
            val = val.strip().strip('"').strip("'")
            if key.strip() == "version":
                found = val
            elif key.strip() == "name":
                name = val
        # Confirm the file describes THIS package before believing it.
        if found and name == "memway":
            return found

    try:
        from importlib.metadata import version as _dist_version

        return _dist_version("memway")
    except Exception:
        return "0+unknown"


__version__ = _derive_version()
