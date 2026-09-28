#!/usr/bin/env python3
"""Knowledge IR test viewer.

Left: conversation files from conversations_output/.
Middle: selected episode rendered as chat bubbles.
Right: play button -> live Knowledge IR extraction -> starfield shard UI.

Run:  python knowledge_viewer.py   (then open http://127.0.0.1:8777)
"""
from __future__ import annotations

import json
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SKILL = Path(__file__).resolve().parents[1]
CONV_DIR = SKILL / "data" / "conversations_output"
IR_DIR = SKILL / "data" / "knowledge_ir"
PORT = 8777

import extract_knowledge_ir as kir

_lock = threading.Lock()
_cache: dict = {"t": 0.0, "files": None}
FNAME_RE = re.compile(r"^[\w.\-]+\.conversations\.jsonl$")

PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Knowledge IR · 星际萃取台</title>
<style>
:root{
  --bg:#05070f; --panel:rgba(13,18,38,.72); --line:rgba(99,102,241,.22);
  --txt:#dbe2ff; --dim:#8b93b8; --acc:#818cf8; --acc2:#22d3ee; --gold:#fbbf24;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--txt);font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;overflow:hidden}
#stars{position:fixed;inset:0;z-index:0}
#app{position:relative;z-index:1;display:grid;grid-template-columns:270px 1fr 460px;height:100vh;gap:10px;padding:10px}
.col{background:var(--panel);border:1px solid var(--line);border-radius:14px;backdrop-filter:blur(8px);display:flex;flex-direction:column;min-height:0;overflow:hidden}
h2{font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);padding:12px 14px 8px}
/* ---- left: file list ---- */
#search{margin:0 12px 10px;padding:7px 10px;background:rgba(2,6,23,.7);border:1px solid var(--line);border-radius:8px;color:var(--txt);outline:none;font-size:13px}
#search:focus{border-color:var(--acc)}
#filelist{overflow-y:auto;flex:1;padding:0 6px 10px}
.fitem{padding:9px 10px;border-radius:9px;cursor:pointer;border:1px solid transparent;margin-bottom:4px;transition:.15s}
.fitem:hover{background:rgba(99,102,241,.12)}
.fitem.active{background:rgba(99,102,241,.2);border-color:var(--acc)}
.fitem .nm{font-size:12.5px;word-break:break-all;line-height:1.35}
.fitem .meta{font-size:11px;color:var(--dim);margin-top:3px;display:flex;gap:8px;align-items:center}
.badge{background:rgba(34,211,238,.15);color:var(--acc2);border:1px solid rgba(34,211,238,.35);border-radius:20px;padding:0 7px;font-size:10px}
/* ---- middle: chat ---- */
#chathead{padding:14px 18px;border-bottom:1px solid var(--line)}
#chathead .t{font-size:16px;font-weight:600}
#chathead .s{font-size:11.5px;color:var(--dim);margin-top:4px}
#eptabs{display:flex;gap:6px;padding:8px 14px 0;flex-wrap:wrap}
.eptab{font-size:11px;padding:4px 10px;border-radius:16px;border:1px solid var(--line);cursor:pointer;color:var(--dim)}
.eptab.active{color:#fff;background:rgba(99,102,241,.3);border-color:var(--acc)}
#chat{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:78%;padding:10px 14px;border-radius:14px;font-size:13.5px;line-height:1.55;white-space:pre-wrap;word-break:break-word;animation:pop .25s ease}
.msg .who{font-size:10.5px;letter-spacing:.1em;margin-bottom:4px;opacity:.75}
.msg .ts{font-size:10px;opacity:.5;margin-top:5px}
.msg.user{align-self:flex-end;background:linear-gradient(135deg,#4f46e5,#7c3aed);border-bottom-right-radius:4px;box-shadow:0 4px 18px rgba(99,102,241,.35)}
.msg.assistant{align-self:flex-start;background:rgba(30,41,59,.85);border:1px solid rgba(148,163,184,.15);border-bottom-left-radius:4px}
.empty{margin:auto;text-align:center;color:var(--dim);font-size:13px;line-height:2}
/* ---- right: knowledge ---- */
#right{display:flex;flex-direction:column}
#playzone{padding:16px;border-bottom:1px solid var(--line);text-align:center}
#play{position:relative;width:100%;padding:13px 0;border:none;border-radius:12px;cursor:pointer;font-size:15px;font-weight:700;letter-spacing:.12em;color:#fff;background:linear-gradient(135deg,#6366f1,#06b6d4);box-shadow:0 0 24px rgba(99,102,241,.5);transition:.2s}
#play:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 0 34px rgba(34,211,238,.6)}
#play:disabled{opacity:.45;cursor:wait}
#stage{font-size:11.5px;color:var(--acc2);margin-top:9px;min-height:16px;font-family:Consolas,monospace}
#knowledge{flex:1;overflow-y:auto;padding:14px}
.kh .kt{font-size:17px;font-weight:700;line-height:1.4}
.kh .kg{font-size:12.5px;color:var(--acc2);margin-top:8px;padding:9px 11px;background:rgba(34,211,238,.07);border-left:3px solid var(--acc2);border-radius:0 8px 8px 0;line-height:1.5}
.kh .ks{font-size:12.5px;color:var(--dim);margin-top:8px;line-height:1.6}
.krow{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
.pill{font-size:11px;padding:3px 11px;border-radius:20px;border:1px solid;font-weight:600}
.pill.completed{color:#4ade80;border-color:#4ade8066;background:#4ade8012}
.pill.partial{color:#fbbf24;border-color:#fbbf2466;background:#fbbf2412}
.pill.blocked,.pill.abandoned{color:#f87171;border-color:#f8717166;background:#f8717112}
.pill.unknown{color:#94a3b8;border-color:#94a3b866;background:#94a3b812}
.stars{color:var(--gold);font-size:15px;letter-spacing:2px;text-shadow:0 0 8px rgba(251,191,36,.6)}
.shard{margin-top:12px;border:1px solid var(--line);border-radius:11px;padding:11px 12px;background:rgba(15,23,42,.55);animation:rise .5s ease both}
.shard .sh-t{font-size:12px;font-weight:700;letter-spacing:.1em;display:flex;align-items:center;gap:7px;margin-bottom:8px}
.shard .cnt{margin-left:auto;font-size:10px;background:rgba(99,102,241,.25);padding:1px 8px;border-radius:12px}
.kitem{font-size:12.5px;line-height:1.55;padding:7px 9px;border-radius:8px;margin-bottom:6px;background:rgba(2,6,23,.45);border:1px solid rgba(99,102,241,.12)}
.kitem .meta{display:flex;gap:8px;margin-top:5px;font-size:10px;color:var(--dim);align-items:center}
.conf{width:7px;height:7px;border-radius:50%;display:inline-block}
.conf.high{background:#4ade80;box-shadow:0 0 6px #4ade80}
.conf.medium{background:#fbbf24;box-shadow:0 0 6px #fbbf24}
.conf.low{background:#f87171;box-shadow:0 0 6px #f87171}
.ev{color:#6366f1;font-family:Consolas,monospace}
.ttag{font-size:9.5px;border:1px solid var(--line);border-radius:10px;padding:0 6px;color:var(--dim)}
.chip{display:inline-block;font-size:11px;padding:3px 10px;border-radius:16px;margin:0 6px 6px 0;border:1px solid;background:rgba(2,6,23,.5)}
.chip .ty{opacity:.6;font-size:9.5px;margin-left:5px}
.cue{display:inline-block;font-size:11px;padding:3px 10px;border-radius:6px;margin:0 6px 6px 0;background:rgba(34,211,238,.08);border:1px dashed rgba(34,211,238,.4);color:#a5f3fc}
.loc{font-size:10.5px;color:var(--dim);word-break:break-all}
/* loader */
.orb{width:54px;height:54px;margin:26px auto 8px;position:relative}
.orb .core{position:absolute;inset:18px;border-radius:50%;background:radial-gradient(circle,#22d3ee,#6366f1);box-shadow:0 0 22px #6366f1;animation:pulse 1.4s infinite}
.orb .ring{position:absolute;inset:0;border-radius:50%;border:1.5px solid transparent;border-top-color:#22d3ee;border-right-color:#6366f1;animation:spin 1.1s linear infinite}
.orb .ring.r2{inset:6px;animation:spin 1.8s linear infinite reverse;border-top-color:#fbbf24;border-right-color:transparent}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse{50%{transform:scale(.78);opacity:.8}}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes pop{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:none}}
::-webkit-scrollbar{width:8px}::-webkit-scrollbar-thumb{background:rgba(99,102,241,.3);border-radius:4px}
</style>
</head>
<body>
<canvas id="stars"></canvas>
<div id="app">
  <div class="col">
    <h2>◈ Conversations</h2>
    <input id="search" placeholder="搜索 session / 标题…">
    <div id="filelist"><div class="empty">加载中…</div></div>
  </div>
  <div class="col">
    <div id="chathead"><div class="t">—</div><div class="s">选择左侧文件查看对话</div></div>
    <div id="eptabs"></div>
    <div id="chat"><div class="empty">☄<br>等待选择</div></div>
  </div>
  <div class="col" id="right">
    <div id="playzone">
      <button id="play" disabled>▶ 萃取知识</button>
      <div id="stage"></div>
    </div>
    <div id="knowledge"><div class="empty">✦<br>选中对话后点击 ▶<br>开始知识萃取</div></div>
  </div>
</div>
<script>
/* ---------- starfield ---------- */
const cv=document.getElementById('stars'),cx=cv.getContext('2d');let stars=[];
function initStars(){cv.width=innerWidth;cv.height=innerHeight;stars=Array.from({length:190},()=>({x:Math.random()*cv.width,y:Math.random()*cv.height,r:Math.random()*1.4+.3,p:Math.random()*Math.PI*2,s:.008+Math.random()*.02}))}
initStars();addEventListener('resize',initStars);
(function tick(){cx.clearRect(0,0,cv.width,cv.height);for(const s of stars){s.p+=s.s;const a=.25+.55*Math.abs(Math.sin(s.p));cx.beginPath();cx.arc(s.x,s.y,s.r,0,7);cx.fillStyle=`rgba(165,180,252,${a})`;cx.fill()}requestAnimationFrame(tick)})();

/* ---------- state ---------- */
let files=[],curFile=null,curEp=0,episodes=[],busy=false;
const $=id=>document.getElementById(id);
const esc=s=>{const d=document.createElement('div');d.textContent=s??'';return d.innerHTML};

/* ---------- file list ---------- */
async function loadFiles(){
  const r=await fetch('/api/files');files=await r.json();renderFiles();
}
function renderFiles(){
  const q=$('search').value.toLowerCase();
  const list=files.filter(f=>!q||f.name.toLowerCase().includes(q)||f.titles.some(t=>(t||'').toLowerCase().includes(q)));
  $('filelist').innerHTML=list.map(f=>{
    const agent=f.name.split('_')[0];
    const icons={claude:'🟠',codex:'🟢',pi:'🔵',grok:'⚪',hermes:'🟣',openclaw:'🔴'};
    return `<div class="fitem ${f.name===curFile?'active':''}" onclick="selFile('${f.name}')">
      <div class="nm">${icons[agent]||'⚫'} ${esc(f.name.replace('.conversations.jsonl',''))}</div>
      <div class="meta"><span class="badge">${f.episodes} ep</span><span>${f.msgs} msg</span></div>
      <div class="meta" style="margin-top:2px">${esc((f.titles[0]||'').slice(0,42))}</div>
    </div>`}).join('')||'<div class="empty">无匹配</div>';
}
$('search').addEventListener('input',renderFiles);

/* ---------- chat ---------- */
async function selFile(name){
  curFile=name;curEp=0;renderFiles();
  const r=await fetch(`/api/episode?file=${encodeURIComponent(name)}&idx=0`);
  const d=await r.json();episodes=d.episodes;renderEpisode();
}
function renderEpisode(){
  const ep=episodes[curEp];if(!ep)return;
  $('chathead').innerHTML=`<div class="t">${esc(ep.title||'(无标题)')}</div>
    <div class="s">${esc(curFile)} · ${ep.messages.length} 条消息 · L${ep.start_line_no}–${ep.end_line_no}</div>`;
  $('eptabs').innerHTML=episodes.length>1?episodes.map((e,i)=>`<div class="eptab ${i===curEp?'active':''}" title="${esc(e.title||'')}" onclick="curEp=${i};renderEpisode()">C${i+1}</div>`).join(''):'';
  $('chat').innerHTML=ep.messages.map(m=>`<div class="msg ${m.role==='user'?'user':'assistant'}">
    <div class="who">${m.role==='user'?'👤 USER':'🤖 ASSISTANT'} · #${m.line_no}</div>${esc(m.text)}
    <div class="ts">${esc(m.ts||'')}</div></div>`).join('')||'<div class="empty">空对话</div>';
  $('chat').scrollTop=0;
  $('play').disabled=false;
  $('knowledge').innerHTML='<div class="empty">✦<br>点击 ▶ 开始萃取<br>当前对话的知识碎片</div>';
  $('stage').textContent='';
}
window.selFile=selFile;

/* ---------- extraction ---------- */
const STAGES=['📡 读取对话星尘…','🛰 召唤分析模型…','🧠 提炼认知结构…','💠 凝结知识碎片…'];
$('play').onclick=async()=>{
  if(busy||!curFile)return;busy=true;
  $('play').disabled=true;$('play').textContent='⏳ 萃取中…';
  $('knowledge').innerHTML='<div class="orb"><div class="ring"></div><div class="ring r2"></div><div class="core"></div></div>';
  let si=0,t0=Date.now();
  const iv=setInterval(()=>{$('stage').textContent=`${STAGES[si++%STAGES.length]}  ${( (Date.now()-t0)/1000).toFixed(0)}s`},900);
  try{
    const r=await fetch('/api/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:curFile,idx:curEp})});
    const d=await r.json();
    clearInterval(iv);
    if(!r.ok){$('stage').textContent='⚠ '+(d.error||'萃取失败');}
    else{$('stage').textContent=`✓ 完成 ${(d.elapsed_ms/1000).toFixed(1)}s`;renderIR(d.ir);}
  }catch(e){clearInterval(iv);$('stage').textContent='⚠ '+e.message;}
  busy=false;$('play').disabled=false;$('play').textContent='▶ 重新萃取';
};

const CONF=c=>`<span class="conf ${c}"></span>${c}`;
const EV=ev=>ev&&ev.length?`<span class="ev">L${ev.join(',')}</span>`:'';
function itemHTML(it,extra=''){return `<div class="kitem">${esc(it.text||'')}
  <div class="meta">${CONF(it.confidence||'medium')}${EV(it.evidence)}${extra}</div></div>`}

function renderIR(ir){
  const imp={high:3,medium:2,low:1}[ir.importance]||2;
  const secs=[
    ['findings','🔍 FINDINGS',ir.findings,i=>itemHTML(i)],
    ['decisions','⚖️ DECISIONS',ir.decisions,i=>itemHTML(i)],
    ['ideas','💡 IDEAS',ir.ideas,i=>itemHTML(i)],
    ['open_questions','❓ OPEN QUESTIONS',ir.open_questions,i=>itemHTML(i)],
    ['tasks','✅ TASKS',ir.tasks,i=>itemHTML(i,`<span class="ttag">${esc(i.type||'')}</span>`)],
    ['constraints','⛓ CONSTRAINTS',ir.constraints,i=>itemHTML(i)],
    ['lessons','🧠 LESSONS',ir.lessons,i=>itemHTML(i)],
  ];
  let h=`<div class="kh" style="animation:rise .4s ease both">
    <div class="kt">${esc(ir.title)}</div>
    <div class="kg">🎯 ${esc(ir.goal)}</div>
    <div class="ks">${esc(ir.summary)}</div>
    <div class="krow">
      <span class="pill ${ir.outcome?.status||'unknown'}">${(ir.outcome?.status||'?').toUpperCase()}</span>
      <span class="stars">${'★'.repeat(imp)}${'☆'.repeat(3-imp)} <span style="font-size:10px;color:var(--dim)">${esc(ir.importance)}</span></span>
    </div>
    ${ir.outcome?.result?`<div class="ks" style="margin-top:6px">📌 ${esc(ir.outcome.result)}</div>`:''}
  </div>`;
  let d=0;
  for(const[k,label,arr,fn]of secs){
    if(!arr||!arr.length)continue;
    h+=`<div class="shard" style="animation-delay:${(d++)*0.09}s"><div class="sh-t">${label}<span class="cnt">${arr.length}</span></div>${arr.map(fn).join('')}</div>`;
  }
  if(ir.artifacts?.length){
    h+=`<div class="shard" style="animation-delay:${(d++)*0.09}s"><div class="sh-t">📦 ARTIFACTS<span class="cnt">${ir.artifacts.length}</span></div>`+
      ir.artifacts.map(a=>`<div class="kitem"><b>${esc(a.name)}</b> <span class="ttag">${esc(a.type)}</span> <span class="ttag">${esc(a.action)}</span>
        ${a.location?`<div class="loc">📍 ${esc(a.location)}</div>`:''}
        <div class="meta">${CONF(a.confidence||'medium')}${EV(a.evidence)}</div></div>`).join('')+'</div>';
  }
  if(ir.entities?.length){
    const cols={project:'#818cf8',tool:'#22d3ee',service:'#34d399',model:'#f472b6',file:'#fbbf24',person:'#fb923c',concept:'#a78bfa'};
    h+=`<div class="shard" style="animation-delay:${(d++)*0.09}s"><div class="sh-t">✨ ENTITIES<span class="cnt">${ir.entities.length}</span></div>`+
      ir.entities.map(e=>`<span class="chip" style="border-color:${cols[e.type]||'#64748b'}88">${esc(e.name)}<span class="ty">${esc(e.type)}</span></span>`).join('')+'</div>';
  }
  if(ir.retrieval_cues?.length){
    h+=`<div class="shard" style="animation-delay:${(d++)*0.09}s"><div class="sh-t">🏷 RETRIEVAL CUES<span class="cnt">${ir.retrieval_cues.length}</span></div>`+
      ir.retrieval_cues.map(c=>`<span class="cue">${esc(c)}</span>`).join('')+'</div>';
  }
  $('knowledge').innerHTML=h;
  $('knowledge').scrollTop=0;
}
loadFiles();
</script>
</body>
</html>
"""


def _list_files() -> list[dict]:
    now = time.time()
    with _lock:
        if _cache["files"] is not None and now - _cache["t"] < 10:
            return _cache["files"]
        out = []
        for p in sorted(CONV_DIR.glob("*.conversations.jsonl")):
            n_eps = 0
            n_msgs = 0
            titles = []
            with p.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    n_eps += 1
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    n_msgs += len(obj.get("messages") or [])
                    if len(titles) < 3 and obj.get("title"):
                        titles.append(obj["title"])
            out.append({
                "name": p.name,
                "episodes": n_eps,
                "msgs": n_msgs,
                "titles": titles,
            })
        _cache["files"] = out
        _cache["t"] = now
        return out


def _resolve_file(name: str) -> Path:
    if not FNAME_RE.match(name):
        raise ValueError("bad file name")
    p = CONV_DIR / name
    if not p.exists():
        raise ValueError("file not found")
    return p


def _upsert_ir(src: Path, ir: dict) -> Path:
    """Insert/replace this conversation's IR line in the file's knowledge.jsonl."""
    IR_DIR.mkdir(exist_ok=True)
    out = kir.default_out_path(src, IR_DIR)
    rows = []
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("conversation_id") != ir.get("conversation_id"):
                rows.append(obj)
    rows.append(ir)
    rows.sort(key=lambda o: str(o.get("conversation_id") or ""))
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/files":
                self._json(_list_files())
            elif u.path == "/api/episode":
                q = parse_qs(u.query)
                p = _resolve_file(q.get("file", [""])[0])
                convs = kir.load_conversations(p)
                eps = [{
                    "conversation_id": c.get("conversation_id"),
                    "title": c.get("title"),
                    "start_line_no": c.get("start_line_no"),
                    "end_line_no": c.get("end_line_no"),
                    "messages": c.get("messages") or [],
                } for c in convs]
                self._json({"episodes": eps})
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/extract":
            self._send(404, b"not found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            p = _resolve_file(str(body.get("file") or ""))
            idx = int(body.get("idx") or 0)
            convs = kir.load_conversations(p)
            if not (0 <= idx < len(convs)):
                raise ValueError("episode index out of range")
            t0 = time.time()
            ir = kir.extract_one(convs[idx])
            out = _upsert_ir(p, ir)
            self._json({
                "ir": ir,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "saved_to": str(out),
            })
        except Exception as e:
            self._json({"error": str(e)[:500]}, 500)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Knowledge IR viewer → {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    srv.serve_forever()


if __name__ == "__main__":
    main()
