"""The usage text is a promise. This makes it one.

`memway viz --force` was printed in viz's own usage line and rejected by
the parser with "applies to 'pull' only" - for as long as --force has
existed. Nothing caught it because nothing ever compared the two: the
help text lived in a docstring and the flag table lived in main(), and
neither knew the other was there.

So: read the flags OUT of the shipped usage text, and require each one to
be receivable by the command it is printed under. A flag can be received
two ways - lifted by main() into a keyword, or parsed by the command out
of its own *args - and this accepts either, because both really work.
"""

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import cli


def documented_flags() -> dict:
    """{command: {flag, ...}} read out of the usage text, not hand-listed.

    Usage entries start with `memway <cmd>` and continue on indented
    lines; a flag on a continuation line belongs to the entry above it.
    Lines that start a new entry reset the ownership, which is what keeps
    pull's --force from being attributed to console.
    """
    out, cur = {}, None
    for line in (cli.__doc__ or "").splitlines():
        m = re.match(r"\s*memway ([a-z-]+)", line)
        if m and m.group(1) in cli.COMMANDS:
            cur = m.group(1)
            out.setdefault(cur, set())
        elif m or (line.strip() and not line.startswith("  ")):
            cur = None                      # a heading, or `memway --json`
        if cur:
            for f in re.findall(r"--[a-z][a-z-]*", line):
                out[cur].add(f)
    return out


def test_the_usage_text_actually_advertises_flags():
    """Guard the guard: if the parse returns nothing, every check below
    passes vacuously and the pin is decoration."""
    d = documented_flags()
    assert len(d) >= 5, d
    assert d.get("viz", set()) >= {"--out", "--filter", "--force"}, d.get("viz")
    assert "--force" in d.get("pull", set()), d.get("pull")


def test_every_documented_flag_is_receivable():
    """The pin. Two legal routes in, and no third."""
    import inspect
    src = HERE / "memway" / "cli.py"
    text = src.read_text()
    broken = []
    for cmd, flags in documented_flags().items():
        fn = cli.COMMANDS[cmd]
        body = inspect.getsource(fn)
        for f in sorted(flags):
            owners = cli.VALUE_FLAGS.get(f) or cli.BOOL_FLAGS.get(f)
            if owners is not None:
                if cmd not in owners:
                    broken.append(f"{cmd} {f}: main() gives it to "
                                  f"{' or '.join(owners)} only")
                continue
            if f'"{f}"' not in body and f"'{f}'" not in body:
                broken.append(f"{cmd} {f}: not in the flag table and "
                              f"{fn.__name__} does not parse it")
    assert not broken, "usage text promises flags the parser refuses:\n  " + \
        "\n  ".join(broken)


def _run(*args):
    return subprocess.run([sys.executable, "-m", "memway.cli", *args],
                          capture_output=True, text=True, cwd=str(HERE))


def test_viz_force_is_accepted_by_the_real_cli(tmp_path):
    """Executed, not inferred - the bug was in dispatch, and dispatch is
    the one thing a static read of the table cannot exercise."""
    out = tmp_path / "m.html"
    r = _run("viz", str(HERE), "--out", str(out), "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "applies to" not in r.stderr, r.stderr
    assert out.exists() and out.stat().st_size > 10_000


def test_a_flag_on_the_wrong_command_still_fails():
    """The ownership check has to keep MEANING something. A fix that
    accepted --force everywhere would pass every test above."""
    r = _run("show", str(HERE), "--force")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "applies to" in r.stderr, r.stderr
    assert "'pull' or 'viz'" in r.stderr, r.stderr


# --------------------------------------- a typo is not a request for help

def test_unknown_command_names_the_word_it_did_not_understand():
    """`memway freshness .` printed the entire quickstart and never said
    'freshness'. The reader has to diff the command list by eye to learn
    which word was wrong - on the one path where they already know they
    made a mistake and just want to be told which."""
    r = _run("freshness", ".")
    out = r.stdout + r.stderr
    assert r.returncode == 1, r.returncode
    assert "freshness" in out, f"never named the bad command:\n{out}"
    assert "Quickstart" not in out, f"dumped the full help:\n{out}"
    assert len(out.strip().splitlines()) == 1, \
        f"expected one line, got:\n{out}"
    assert not r.stdout.strip(), \
        f"an error belongs on stderr, not stdout: {r.stdout!r}"


def test_a_near_miss_gets_the_command_it_probably_meant():
    """Asserts the SUGGESTION, not the word.

    The first version of this checked `"summary" in out`, which the full
    help dump satisfies - the command list contains every command. It
    passed against the bug it was written to catch.
    """
    r = _run("summry", ".")
    out = (r.stdout + r.stderr).strip()
    assert "did you mean 'summary'" in out, f"no suggestion offered:\n{out}"
    assert len(out.splitlines()) == 1, f"expected one line, got:\n{out}"


def test_a_wild_miss_says_where_the_list_is_rather_than_guessing():
    """Same trap: every assertion here is also true of the help dump
    unless the one-line shape is pinned too."""
    r = _run("zzzzz")
    out = (r.stdout + r.stderr).strip()
    assert len(out.splitlines()) == 1, f"expected one line, got:\n{out}"
    assert "zzzzz" in out, out
    assert "did you mean" not in out, f"guessed at nonsense:\n{out}"


def test_bare_invocation_still_prints_the_whole_map():
    """The other half of the branch: no command IS a request for help."""
    r = _run()
    out = r.stdout + r.stderr
    assert r.returncode == 1
    assert "Quickstart" in out, f"bare invocation lost its help:\n{out[:300]}"


# ------------------------------------- probe says which mistake you made

def test_probe_rejects_wrong_arity_at_the_boundary(tmp_path):
    """bind() BEFORE the call, the same rule cli.main follows: catching
    TypeError around the call would swallow a TypeError raised inside
    working code and blame the caller for it.

    Reported by an agent evaluating the tool - it passed the wrong shape
    and got an exception three frames down, with nothing naming the
    signature it should have matched.
    """
    import subprocess
    from memway.query import probe

    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text(
        "def scale(items, factor):\n    return [i * factor for i in items]\n")
    subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"],
                   check=True)
    assert _run("init", str(r)).returncode == 0

    out = probe(str(r), "scale", args=[[1, 2, 3]])
    assert out.get("ok") is False, out
    assert "arguments do not match" in out.get("error", ""), out
    assert out.get("signature", "").endswith("(items, factor)"), out
    assert "setup" in out.get("hint", ""), out


def test_probe_points_at_types_when_the_target_itself_raises(tmp_path):
    """Arity matched, so bind() cannot help - but the first frame IS the
    target, which is the signal that the VALUES are wrong rather than the
    code. "You called it wrong" and "it is broken" must not read alike.

    TWO REPOS IN ONE PROCESS, deliberately. probe imports by module name
    and sys.modules is per-process, so a second repo containing `m.py`
    used to get the FIRST repo's module, run its code, and produce frames
    _locate could not match - losing raised_at entirely. Probing one repo
    would pass against that bug; the collision is the test.
    """
    import subprocess
    from memway.query import probe

    SRC = "def scale(items, factor):\n    return [i * factor for i in items]\n"
    made = []
    for name in ("first", "second"):
        r = tmp_path / name
        r.mkdir()
        (r / "m.py").write_text(SRC)
        subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"],
                       check=True)
        assert _run("init", str(r)).returncode == 0
        made.append(r)

    probe(str(made[0]), "scale", args=[[1, 2], 2])      # caches `m` from repo 1

    out = probe(str(made[1]), "scale", args=["notalist", None])
    ex = out.get("exception", {})
    assert out.get("ok") is False, out
    assert ex.get("raised_at", "").startswith("m.scale"), (
        f"coordinate attribution was lost - the cached module from another "
        f"repo shadowed this one: {ex}")
    assert ex.get("signature", "").endswith("(items, factor)"), ex
    assert "TYPES" in ex.get("hint", ""), ex


def test_probe_still_returns_a_normal_result_for_a_good_call(tmp_path):
    """The other half: none of the above may fire on a correct call."""
    import subprocess
    from memway.query import probe

    r = tmp_path / "p"
    r.mkdir()
    (r / "m.py").write_text(
        "def scale(items, factor):\n    return [i * factor for i in items]\n")
    subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"],
                   check=True)
    assert _run("init", str(r)).returncode == 0

    out = probe(str(r), "scale", args=[[1, 2, 3], 2])
    assert out.get("ok") is True, out
    assert "error" not in out and "hint" not in out, out
