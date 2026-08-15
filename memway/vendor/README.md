# Vendored assets

Third-party files shipped verbatim so that emitted HTML makes **zero**
network requests. `tests/test_airgap.py` asserts that property on the
emitted bytes of both `viz` and `console`; if you add an asset here, it
must be inlined, never linked.

| file | version | license | upstream |
|---|---|---|---|
| `d3.min.js` | 7.8.5 | ISC (`d3.LICENSE`) | https://d3js.org |

d3 is **ISC**, not BSD — permissive either way, but the notice in
`d3.LICENSE` must ship with any copy, and the minified file keeps its
own `// https://d3js.org v7.8.5 Copyright 2010-2023 Mike Bostock`
header. Do not strip either.

Updating: replace the file, update the version in this table, and run
the airgap tests. There is no build step and no bundler on purpose.
