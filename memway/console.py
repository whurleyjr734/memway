"""
Console: the map explorer served live, with the read tools as buttons.

Stdlib only (http.server). The page is viz's approved explorer plus a
tool rail; the server is a thin shell over the SAME query functions the
MCP server calls, so the browser and the agent can never drift.

SECURITY POSTURE
================

This process exposes a WRITE path (POST /api/meta). Two controls, both
non-negotiable and both tested:

  1. Bound to 127.0.0.1 ONLY. Never 0.0.0.0 - that would publish a
     write endpoint to the local network.
  2. A random per-session token. EVERY request must carry it. Without
     this, any webpage the user happens to have open could POST to
     localhost and write notes into their map: browsers allow
     cross-origin POSTs, and "it's only localhost" is not a boundary.
     401 is the default answer; authorisation is the exception.

DELIBERATELY ABSENT
===================

  probe    - executes repository code. A browser button that runs
             arbitrary repo code is a different trust model entirely;
             not in v1, and tests assert the endpoint does not exist.
  index    - a write that rebuilds the map; the console is for reading
             and annotating, not rebuilding.
  attention- v2.

THE FENCE
=========

Every GET leaves .coord byte-identical (log/ excepted - the flight
recorder is personal-machine telemetry). That holds because queries run
inside `query.read_only()`, which stops BOTH pickle caches from being
warmed. There were two, and the viz build found the second one only
after the first was fixed.

POST /api/meta writes exactly one entry and returns a receipt saying so.

HONESTY
=======

Knowledge is live - a note written through the console appears on the
next /api/map poll. STRUCTURE is as of the last index: staleness is
computed against the hashes stored in the map, so editing code does not
change a stamp until `memway index` runs. The footer says exactly that
rather than letting a live-updating page imply more freshness than it
has.
"""

import html
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HOST = "127.0.0.1"          # never 0.0.0.0 - see the posture note above
READ_TOOLS = ("summary", "show", "before_edit", "lineage", "dig")
# Named so the refusal is explicit rather than an accident of routing.
EXCLUDED_TOOLS = ("probe", "index", "reindex", "attention", "verify_change")
MAX_BODY = 64 * 1024


def _tool_call(repo: str, name: str, ref: str) -> dict:
    """Dispatch to the same functions the MCP server uses."""
    from . import query
    with query.read_only():
        if name == "summary":
            return query.summary(repo)
        if not ref:
            return {"error": f"{name} requires ?ref="}
        if name == "show":
            return query.show(repo, ref)
        if name == "before_edit":
            return query.before_edit(repo, ref)
        if name == "lineage":
            return query.lineage(repo, ref)
        if name == "dig":
            from .dig import dig, MCP_CAP_BYTES
            return dig(repo, ref, cap_bytes=MCP_CAP_BYTES)
    return {"error": f"unknown tool {name!r}"}


def _map_payload(repo: str) -> dict:
    from . import query
    from .viz import export
    with query.read_only():
        p = export(repo, force=True)
    p.pop("_census", None)
    return p


def build_page(repo: str, token: str) -> str:
    """viz's template, plus the console shell, with the token baked in."""
    from .viz import TEMPLATE, PLACEHOLDER
    html_doc = TEMPLATE.read_text()
    blob = json.dumps(_map_payload(repo)).replace("</", "<\\/")
    html_doc = html_doc.replace(PLACEHOLDER, blob)
    js = _CONSOLE_JS.replace("__TOKEN__", json.dumps(token))
    return html_doc.replace("</body>", js + "\n</body>")


class Handler(BaseHTTPRequestHandler):
    repo = "."
    token = ""

    def log_message(self, *a):
        pass                                    # no request spam on stdout

    # ----------------------------------------------------------- helpers
    def _send(self, code: int, payload, ctype="application/json"):
        body = (payload if isinstance(payload, bytes)
                else (payload if isinstance(payload, str)
                      else json.dumps(payload)).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The page never embeds remote content beyond D3, and nothing
        # here should be framed by another origin.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self, q: dict) -> bool:
        got = (q.get("token", [""])[0]
               or self.headers.get("X-Memway-Token", ""))
        # compare_digest: a plain == leaks length/prefix timing. Cheap
        # here, and the habit is the point.
        return bool(self.token) and secrets.compare_digest(got, self.token)

    # --------------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authorised(q):
            return self._send(401, {"error": "missing or bad session token"})
        if u.path in ("/", "/index.html"):
            return self._send(200, build_page(self.repo, self.token),
                              "text/html; charset=utf-8")
        if u.path == "/api/map":
            return self._send(200, _map_payload(self.repo))
        m = re.fullmatch(r"/api/tool/([a-z_]+)", u.path)
        if m:
            name = m.group(1)
            if name in EXCLUDED_TOOLS or name not in READ_TOOLS:
                return self._send(404, {
                    "error": f"no endpoint for {name!r}",
                    "available": list(READ_TOOLS),
                    "excluded": list(EXCLUDED_TOOLS)})
            ref = q.get("ref", [""])[0]
            return self._send(200, _tool_call(self.repo, name, ref))
        return self._send(404, {"error": "no such endpoint"})

    # -------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authorised(q):
            return self._send(401, {"error": "missing or bad session token"})
        if u.path != "/api/meta":
            return self._send(404, {"error": "the only write is /api/meta"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self._send(400, {"error": "bad or oversized body"})
        try:
            data = json.loads(self.rfile.read(n))
        except ValueError:
            return self._send(400, {"error": "body must be JSON"})
        return self._send(*_write_meta(self.repo, data))


def _write_meta(repo: str, data: dict):
    """The ONE write. Exactly one entry, and a receipt saying so."""
    from .indexer import Indexer
    from .metadata import MetaStore, CHANNELS
    ref = (data.get("ref") or "").strip()
    channel = (data.get("channel") or "notes").strip()
    text = (data.get("text") or "").strip()
    if not ref or not text:
        return 400, {"error": "ref and text are required"}
    if channel not in CHANNELS:
        return 400, {"error": f"unknown channel {channel!r}",
                     "channels": list(CHANNELS)}
    repo_p = Path(repo).resolve()
    coord = repo_p / ".coord"
    ix = Indexer(repo_p, coord)
    ix.load_existing(write_cache=False)
    e = ix.resolve(ref)
    if e is None:
        return 404, {"error": f"{ref!r} does not resolve"}
    store = MetaStore(coord)
    path = store.root / e.coord_id / f"{channel}.jsonl"
    before = len(path.read_text().splitlines()) if path.exists() else 0
    store.add(e.coord_id, channel, text, author="console",
              body_hash=e.body_hash)
    after = len(path.read_text().splitlines())
    return 200, {
        "ok": True, "coord_id": e.coord_id, "qualname": e.qualname,
        "channel": channel,
        "entries_written": after - before,      # receipt: must be exactly 1
        "channel_total": after,
    }


def serve(repo: str, port: int = 0, open_browser: bool = True):
    """Start the console. Returns (server, url, thread)."""
    token = secrets.token_urlsafe(32)
    handler = type("BoundHandler", (Handler,),
                   {"repo": str(Path(repo).resolve()), "token": token})
    httpd = ThreadingHTTPServer((HOST, port), handler)
    url = f"http://{HOST}:{httpd.server_address[1]}/?token={token}"
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    return httpd, url, t


# --------------------------------------------------------------------- UI

_CONSOLE_JS = r"""
<style>
  /* mw- prefix is load-bearing: the template already owns .rail (the
     fixed filters panel) and .card/.note/.seal. An injected class that
     collides inherits position:fixed and leaves the card entirely. */
  .mw-rail{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 4px}
  .mw-rail button{font:inherit;font-size:11px;letter-spacing:.04em;
    text-transform:uppercase;padding:5px 9px;cursor:pointer;
    background:#1b1a17;color:#e9e2d0;border:1px solid #3a352c;border-radius:3px}
  .mw-rail button:hover{border-color:var(--amber);color:var(--amber)}
  .mw-rail button[disabled]{opacity:.5;cursor:progress}
  .mw-rail button.active{border-color:var(--amber);color:var(--amber);
    background:#241f16}
  /* NO max-height/overflow here: the panel already scrolls, and a
     scroller inside a scroller traps the wheel and shows two bars -
     that is the 'janky' feel. One scroll context, the panel's. */
  .toolout{margin-top:8px;font-size:12px}
  .toolout pre{white-space:pre-wrap;word-break:break-word;margin:0;
    max-height:240px;overflow:auto}
  .toolhead{display:flex;align-items:center;justify-content:space-between;
    gap:8px;margin:6px 0 4px}
  .toolhead .who{font-family:'IBM Plex Mono',monospace;font-size:10px;
    letter-spacing:.16em;text-transform:uppercase;color:var(--amber)}
  .toolhead .dismiss{background:none;border:0;color:#8a8272;font-size:16px;
    line-height:1;cursor:pointer;padding:0 2px}
  .toolhead .dismiss:hover{color:var(--bone)}
  .cand{border-left:2px solid #3a352c;padding:6px 8px;margin:6px 0}
  .cand .sha{color:var(--amber);font-weight:600}
  .cand .warn{color:#d98b5f;font-size:11px}
  .noteform{margin-top:10px;border-top:1px solid #2a2620;padding-top:8px}
  .noteform textarea{width:100%;min-height:52px;background:#141310;
    color:#e9e2d0;border:1px solid #3a352c;border-radius:3px;padding:6px;
    font:inherit;font-size:12px}
  .noteform .mw-row{display:flex;gap:6px;margin-top:6px;align-items:center}
  .noteform select{background:#141310;color:#e9e2d0;border:1px solid #3a352c;
    border-radius:3px;padding:4px;font:inherit;font-size:11px}
  /* NOT .ok/.err: the template's .err is display:none, so a failed
     stamp would have reported nothing at all. */
  .mw-ok{color:var(--fresh);font-size:11px}
  .mw-err{color:var(--beacon);font-size:11px}
  @keyframes stamppulse{0%{r:0;opacity:.9}100%{r:26;opacity:0}}
  .pulse{fill:none;stroke:var(--amber);stroke-width:2;
    animation:stamppulse .7s ease-out forwards}
  @media (prefers-reduced-motion: reduce){
    .pulse{animation:none;opacity:0}
  }
  .console-foot{color:#8a8272;font-size:11px;margin-top:6px}
</style>
<script>
const TOKEN=__TOKEN__;
const api=(p,o)=>fetch(p+(p.includes("?")?"&":"?")+"token="+encodeURIComponent(TOKEN),o)
  .then(r=>r.json().then(j=>({status:r.status,body:j})));
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>(
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function railFor(d){
  return `<div class="mw-rail">
    ${["before_edit","show","lineage","dig"].map(t=>
      `<button data-tool="${t}" data-ref="${esc(d.id)}">${t.replace("_"," ")}</button>`).join("")}
  </div>
  <div class="toolout" id="toolout"></div>
  <div class="noteform">
    <textarea id="noteText" placeholder="add a note at this coordinate…"></textarea>
    <div class="mw-row">
      <select id="noteChan">
        <option>notes</option><option>docs</option><option>design</option>
        <option>history</option><option>confirm</option><option>traces</option>
      </select>
      <button id="noteSave" data-ref="${esc(d.id)}">stamp</button>
      <span id="noteMsg"></span>
    </div>
  </div>
  <div class="console-foot">live knowledge, indexed structure — reindex via
    CLI to refresh stamps</div>`;
}

function clearTool(){
  const out=document.getElementById("toolout");
  if(out) out.innerHTML="";
  document.querySelectorAll(".mw-rail button[data-tool]")
    .forEach(b=>b.classList.remove("active"));
}

function renderTool(tool,body){
  const out=document.getElementById("toolout");
  if(!out) return;
  const head=`<div class="toolhead"><span class="who">${esc(tool)}</span><button class="dismiss" data-dismiss="1" aria-label="Close result">&times;</button></div>`;
  if(body.error){out.innerHTML=head+`<div class="mw-err">${esc(body.error)}</div>`;return;}
  if(tool==="dig"){
    const cs=body.candidates||[];
    out.innerHTML=head+`<div class="kn-head">${cs.length} commits touched this range</div>`+
      cs.map(c=>`<div class="cand">
        <div><span class="sha">${esc(c.short_sha)}</span> ${esc(c.date)} —
             ${esc(c.subject)}</div>
        ${c.provenance&&c.provenance.indexOf("region")===0?
          `<div class="warn">${esc(c.provenance)}</div>`:""}
        ${(c.pr_refs||[]).filter(r=>r.body).map(r=>
          `<div class="warn">PR #${esc(r.number)}</div>
           <pre>${esc((r.body||"").slice(0,400))}</pre>`).join("")}
        ${(c.warnings||[]).map(w=>`<div class="warn">${esc(w)}</div>`).join("")}
        <button data-stamp="${esc(c.short_sha)}" data-date="${esc(c.date)}"
                data-sub="${esc(c.subject)}">stamp as note</button>
      </div>`).join("");
    return;
  }
  if(tool==="before_edit"){
    const w=(body.warnings||[]).map(x=>`<div class="warn">${esc(x)}</div>`).join("");
    const k=(body.knowledge||[]).map(x=>
      `<div class="note ${x.stale?"stale":""}">
         <div class="seal"><span class="dot"></span>${esc(x.channel||"")}${
           x.stale?" · stale":""}</div>${esc(x.text)}</div>`).join("");
    out.innerHTML=head+(w+k||"<pre>"+esc(JSON.stringify(body,null,1))+"</pre>");
    return;
  }
  out.innerHTML=head+"<pre>"+esc(JSON.stringify(body,null,1))+"</pre>";
}

function pulseRing(id){
  const g=window._refs&&window._refs.node;
  if(!g) return;
  g.filter(d=>d.id===id).each(function(){
    const sel=d3.select(this);
    if(sel.select("circle.stamp-ring").empty()){
      sel.insert("circle",":first-child").attr("class","stamp-ring")
         .attr("r",13);
    }
    // remove on animationend: without this every stamp leaves an
    // invisible circle behind for the life of the page.
    const p=sel.append("circle").attr("class","pulse").attr("r",0);
    const el=p.node();
    el.addEventListener("animationend",()=>el.remove(),{once:true});
    setTimeout(()=>{ if(el.isConnected) el.remove(); },1200);
  });
}

window._consoleRail=railFor;

document.addEventListener("keydown",e=>{
  if(e.key==="Escape" && document.querySelector(".mw-rail button.active")){
    e.stopPropagation(); clearTool();   // first Escape closes the result,
  }                                     // a second closes the panel
},true);

document.addEventListener("click",async ev=>{
  const t=ev.target.closest("button"); if(!t) return;
  if(t.dataset.dismiss){ clearTool(); return; }
  if(t.dataset.tool){
    // clicking the ACTIVE tool closes it. Without this the only way to
    // clear a result was to select a different node, so the card grew
    // and never shrank.
    if(t.classList.contains("active")){ clearTool(); return; }
    clearTool();
    t.classList.add("active");
    t.disabled=true;
    const r=await api(`/api/tool/${t.dataset.tool}?ref=${encodeURIComponent(t.dataset.ref)}`);
    t.disabled=false;
    renderTool(t.dataset.tool,r.body);
    const out=document.getElementById("toolout");
    if(out) out.scrollIntoView({block:"nearest",behavior:"smooth"});
    return;
  }
  if(t.dataset.stamp){
    const ta=document.getElementById("noteText");
    ta.value=`EXCAVATED sha ${t.dataset.stamp} (${t.dataset.date}): `+
             `${t.dataset.sub}`;
    ta.focus(); return;
  }
  if(t.id==="noteSave"){
    const msg=document.getElementById("noteMsg");
    const text=document.getElementById("noteText").value.trim();
    if(!text){msg.className="mw-err";msg.textContent="empty";return;}
    t.disabled=true;
    const r=await api("/api/meta",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ref:t.dataset.ref,
        channel:document.getElementById("noteChan").value,text})});
    t.disabled=false;
    if(r.status===200&&r.body.ok){
      msg.className="mw-ok";
      msg.textContent=`stamped (${r.body.entries_written} entry)`;
      document.getElementById("noteText").value="";
      const m=await api("/api/map");
      if(m.status===200) window._applyLive&&window._applyLive(m.body);
      pulseRing(t.dataset.ref);
    }else{
      msg.className="mw-err"; msg.textContent=esc(r.body.error||"failed");
    }
  }
});
</script>
"""
