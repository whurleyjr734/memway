"""Emitted HTML must make ZERO network requests.

Found by an acceptance sweep, not by this suite: `viz` advertised
"self-contained" in its own module docstring and in the CLI usage line
while the template linked d3 from cdnjs and two stylesheets from Google
Fonts. The console inherited all three, because it renders the same
template.

Why it matters more than offline convenience: a rendered map is a picture
of somebody's private source tree - qualnames, file paths, call structure.
A page that fetches a script announces to a third party that the repo was
opened, and it fails outright on a plane, behind a corporate proxy, or in
any environment that does not have egress. Neither is acceptable for a
tool whose whole premise is that your codebase's memory stays yours.

THE ASSERTION IS ON THE EMITTED BYTES, not on the template. The template
is a source file with a slot in it; what ships to a browser is what viz
writes and what the console serves, and those are what get checked. A
test that read the template would pass while the render path injected
anything it liked.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memway import viz as vizmod
from memway.viz import viz, load_template, D3, D3_SLOT, TEMPLATE

# WHY THIS IS NOT `re.compile("https?://")`
# ==========================================
# The literal "zero http(s):// in the emitted bytes" is unachievable while
# shipping d3 correctly, for two reasons that are both fine:
#
#   1. d3's minified build opens with its own copyright banner,
#      `// https://d3js.org v7.8.5 Copyright 2010-2023 Mike Bostock`.
#      ISC requires that notice to travel with the copy, and
#      test_vendored_d3_ships_with_its_license asserts it survives.
#   2. d3 embeds five W3C namespace URIs. `http://www.w3.org/2000/svg` is
#      the argument to createElementNS - it is an IDENTIFIER, not an
#      address, and nothing ever fetches it. Strip it and the explorer
#      renders no SVG at all.
#
# So the property under test is the one that actually matters: the page
# must issue no requests. That means no FETCH VECTORS - the attributes and
# APIs through which a browser goes to the network - and any absolute URL
# that remains must be one of the known-inert constants, pinned below so a
# new one fails loudly instead of hiding among them.

FETCH_VECTORS = [
    (r"""src\s*=\s*["']?\s*https?:""",        "src= to an absolute URL"),
    (r"""href\s*=\s*["']?\s*https?:""",       "href= to an absolute URL"),
    (r"""srcset\s*=\s*["']?[^"']*https?:""",  "srcset= to an absolute URL"),
    (r"""action\s*=\s*["']?\s*https?:""",     "form action to an absolute URL"),
    (r"""url\(\s*["']?\s*https?:""",          "CSS url() to an absolute URL"),
    (r"@import",                              "CSS @import"),
    (r"@font-face",                           "@font-face (webfont)"),
    (r"""rel\s*=\s*["'](?:preconnect|dns-prefetch|prefetch|preload)""",
                                              "resource hint (preconnect etc.)"),
    (r"\bfetch\s*\(\s*[\"'`]https?:",         "fetch() to an absolute URL"),
    (r"\bXMLHttpRequest\b",                   "XMLHttpRequest"),
    (r"\bimportScripts\s*\(",                 "importScripts()"),
    (r"\bnew\s+WebSocket\b",                  "WebSocket"),
    (r"\bnew\s+EventSource\b",                "EventSource"),
    (r"<iframe\b",                            "<iframe>"),
]

# Absolute URLs allowed to appear as inert text. Anything else is a bug.
INERT_URLS = {
    "https://d3js.org",                        # d3's ISC attribution banner
    "http://www.w3.org/2000/svg",              # createElementNS identifiers
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/2000/xmlns/",
    "http://www.w3.org/XML/1998/namespace",
}

URL_RE = re.compile(r"""https?://[^\s"'`,;)<>\\]*""")

SRC = '''"""Module m."""


def alpha(x):
    """Alpha."""
    return x + 1


class Thing:
    """A thing."""

    def run(self, x):
        return alpha(x)
'''


@pytest.fixture(scope="module")
def mapped(tmp_path_factory):
    R = tmp_path_factory.mktemp("airgap") / "proj"
    R.mkdir()
    subprocess.run(["git", "-C", str(R), "init", "-q", "-b", "main"], check=True)
    (R / "m.py").write_text(SRC)
    subprocess.run(["git", "-C", str(R), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(R), "-c", "user.email=t@t",
                    "-c", "user.name=T", "commit", "-qm", "seed",
                    "--no-gpg-sign"], check=True)
    r = subprocess.run([sys.executable, "-m", "memway.cli", "init", str(R)],
                       capture_output=True, text=True, cwd=str(HERE))
    assert r.returncode == 0, r.stderr[-400:]
    return R


def _offenders(html: str) -> list:
    """Every way this page could reach the network. Empty means airgapped."""
    out = []
    for pat, label in FETCH_VECTORS:
        for m in re.finditer(pat, html, re.I):
            line = html.count("\n", 0, m.start()) + 1
            out.append(f"{label} at line {line}: {m.group(0)[:60]!r}")
    for m in URL_RE.finditer(html):
        if m.group(0).rstrip("/") not in {u.rstrip("/") for u in INERT_URLS}:
            line = html.count("\n", 0, m.start()) + 1
            out.append(f"unrecognised absolute URL at line {line}: {m.group(0)[:80]}")
    return out


# ------------------------------------------------------------------ viz

def test_viz_emits_no_external_references(mapped, tmp_path):
    out = tmp_path / "map.html"
    viz(str(mapped), str(out))
    html = out.read_text()
    assert not _offenders(html), \
        "viz emitted external references:\n  " + "\n  ".join(_offenders(html))


def test_viz_output_actually_contains_d3(mapped, tmp_path):
    """The control. Without it, 'strip every URL' would pass this file."""
    out = tmp_path / "map.html"
    viz(str(mapped), str(out))
    html = out.read_text()
    assert "https://d3js.org v7.8.5" in html, "d3's own banner must survive"
    # A library-only symbol. `forceSimulation` alone is NOT sufficient: the
    # template's own script calls d3.forceSimulation, so it matches whether
    # or not the library was inlined.
    assert "d3=t.d3||{}" in html, \
        "the d3 library itself is absent - the page would render blank"
    assert D3_SLOT not in html, "the slot was never filled"


def test_viz_output_is_larger_than_the_template(mapped, tmp_path):
    """Inlining ~273KB is the point; a shrinking page means a lost asset."""
    out = tmp_path / "map.html"
    viz(str(mapped), str(out))
    assert out.stat().st_size > TEMPLATE.stat().st_size + 200_000


# -------------------------------------------------------------- console

def test_console_page_emits_no_external_references(mapped):
    """The console renders the SAME template and must be equally clean."""
    from memway.console import build_page
    html = build_page(str(mapped), token="test-token")
    assert not _offenders(html), \
        "console served external references:\n  " + "\n  ".join(_offenders(html))
    # NOT `"forceSimulation" in html` - the template's OWN script calls
    # d3.forceSimulation, so that string is present even when the library
    # is missing entirely. It made this assertion vacuous: a console that
    # skipped inlining served a blank page and still passed. The banner is
    # the only string that the LIBRARY carries and the template does not.
    assert "https://d3js.org v7.8.5" in html, "console page lost the d3 library"
    assert D3_SLOT not in html, "console served the page with its slot unfilled"


def test_console_and_viz_share_one_template_reader():
    """Structural: two readers is how the two paths drift apart."""
    src = (HERE / "memway" / "console.py").read_text()
    assert "load_template()" in src
    assert "TEMPLATE.read_text()" not in src, \
        "console must not read the template itself - it would skip inlining"


# ------------------------------------------------------- the vendored asset

def test_vendored_d3_ships_with_its_license():
    assert D3.exists(), "vendored d3 is missing"
    lic = D3.with_name("d3.LICENSE")
    assert lic.exists(), "ISC requires the notice to ship with the copy"
    text = lic.read_text()
    assert "Permission to use, copy, modify" in text
    assert "Mike Bostock" in text
    assert "Copyright 2010-2023 Mike Bostock" in D3.read_text()[:200], \
        "the minified banner carries the notice too - do not strip it"


def test_vendored_d3_cannot_break_out_of_its_script_block():
    assert "</" not in D3.read_text(), \
        "a literal '</' would close the <script> early once inlined"


def test_package_data_ships_the_vendor_directory():
    """A wheel without vendor/ renders a blank page for everyone."""
    py = (HERE / "pyproject.toml").read_text()
    assert "vendor/d3.min.js" in py, "vendored d3 must be in package-data"
    assert "vendor/d3.LICENSE" in py, "the license must ship too"


# ------------------------------------------------------------ no webfonts

def test_no_webfont_families_remain():
    """System stacks only. A @font-face or a named webfont is a request
    waiting to happen even when no URL is visible on the same line."""
    html = TEMPLATE.read_text()
    assert "@font-face" not in html
    for family in ("'Sora'", '"Sora"', "'JetBrains Mono'", '"JetBrains Mono"'):
        assert family not in html, f"{family} is a webfont, not a system face"
    assert "--font-sans:" in html and "--font-mono:" in html
    assert "-apple-system" in html and "ui-monospace" in html
