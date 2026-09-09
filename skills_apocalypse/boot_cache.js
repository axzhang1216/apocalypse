(()=>{
const CACHE_KEY='apocalypse.bootSnapshot.v1';
const SCHEMA=1;
const targetFor=path=>path==='/api/world'?'world':path==='/api/ops'?'ops':null;
const originalFetch=window.fetch.bind(window);
let snapshot=null;
let hasSnapshot=false;
let served=new Set();
let fresh={world:null,ops:null};
let backgroundQueued=false;
let overlay=null,bar=null,label=null,pct=null,retry=null;

function readSnapshot(){
  try{
    const raw=JSON.parse(localStorage.getItem(CACHE_KEY)||'null');
    if(raw&&raw.schema===SCHEMA&&raw.world&&raw.ops){snapshot=raw;hasSnapshot=true;document.documentElement.dataset.apocalypseBoot='cached';}
  }catch(e){console.warn('Apocalypse boot cache unreadable',e)}
}
function saveFresh(){
  if(!fresh.world||!fresh.ops)return;
  try{
    const value={schema:SCHEMA,saved_at:Date.now(),world:fresh.world,ops:fresh.ops};
    localStorage.setItem(CACHE_KEY,JSON.stringify(value));
    snapshot=value;hasSnapshot=true;
  }catch(e){console.warn('Apocalypse boot cache write failed',e)}
}
function ensureOverlay(){
  if(hasSnapshot||overlay)return;
  const style=document.createElement('style');
  style.textContent=`
  .apoc-boot{position:fixed;inset:0;z-index:180;display:flex;align-items:center;justify-content:center;pointer-events:none;background:radial-gradient(circle at 50% 45%,rgba(111,148,184,.07),transparent 28%),rgba(15,19,25,.16);opacity:1;transition:opacity .45s ease}.apoc-boot.done{opacity:0}.apoc-boot.error{pointer-events:auto}.apoc-boot-card{width:min(420px,72vw);transform:translateY(-2vh);font-family:Consolas,monospace}.apoc-boot-word{display:flex;align-items:center;gap:9px;margin-bottom:15px;color:#e6e2da;font-size:12px;font-weight:700;letter-spacing:.14em}.apoc-boot-word i{width:6px;height:6px;border-radius:50%;background:#e48aae;box-shadow:0 0 17px rgba(228,138,174,.85)}.apoc-boot-meta{display:flex;justify-content:space-between;margin-bottom:8px;color:#8f9aa5;font-size:9px;font-weight:600;letter-spacing:.11em}.apoc-boot-track{height:2px;background:rgba(255,255,255,.055);overflow:hidden}.apoc-boot-bar{height:100%;width:6%;background:linear-gradient(90deg,#6f94b8,#e48aae);box-shadow:0 0 14px rgba(111,148,184,.35);transition:width .28s cubic-bezier(.16,.84,.32,1)}.apoc-boot-note{margin-top:9px;color:#59636d;font-size:8px;letter-spacing:.08em}.apoc-boot-retry{display:none;margin-top:14px;padding:7px 10px;border:1px solid rgba(111,148,184,.24);border-radius:4px;background:rgba(111,148,184,.05);color:#b8c3cd;font:9px Consolas,monospace;letter-spacing:.08em;pointer-events:auto}.apoc-boot.error .apoc-boot-retry{display:inline-block}
  `;
  document.head.appendChild(style);
  overlay=document.createElement('div');overlay.className='apoc-boot';overlay.innerHTML=`<div class="apoc-boot-card"><div class="apoc-boot-word"><i></i>APOCALYPSE</div><div class="apoc-boot-meta"><span class="apoc-boot-label">BOOTSTRAP</span><span class="apoc-boot-pct">06%</span></div><div class="apoc-boot-track"><div class="apoc-boot-bar"></div></div><div class="apoc-boot-note">BUILDING LOCAL WORLD STATE</div><button class="apoc-boot-retry">RETRY</button></div>`;
  (document.body||document.documentElement).appendChild(overlay);
  bar=overlay.querySelector('.apoc-boot-bar');label=overlay.querySelector('.apoc-boot-label');pct=overlay.querySelector('.apoc-boot-pct');retry=overlay.querySelector('.apoc-boot-retry');retry.onclick=()=>location.reload();
}
function progress(value,text){
  if(hasSnapshot)return;
  ensureOverlay();
  if(!overlay)return;
  const p=Math.max(0,Math.min(100,Math.round(value)));
  bar.style.width=p+'%';pct.textContent=String(p).padStart(2,'0')+'%';if(text)label.textContent=text;
}
function fail(text){
  if(hasSnapshot)return;
  ensureOverlay();if(!overlay)return;
  overlay.classList.add('error');label.textContent='LOCAL API UNAVAILABLE';pct.textContent='—';overlay.querySelector('.apoc-boot-note').textContent=text||'APOCALYPSE COULD NOT LOAD LOCAL STATE';
}
function finish(){
  if(hasSnapshot&&document.documentElement.dataset.apocalypseBoot==='cached')return;
  progress(100,'WORLD ONLINE');
  setTimeout(()=>{if(!overlay)return;overlay.classList.add('done');setTimeout(()=>overlay.remove(),480)},150);
}
function queueBackgroundRefresh(){
  if(backgroundQueued)return;backgroundQueued=true;
  setTimeout(()=>{
    const fn=window.refreshAll;
    if(typeof fn==='function')Promise.resolve(fn()).catch(e=>console.warn('Apocalypse background refresh failed; cached snapshot remains visible',e));
  },90);
}

readSnapshot();
if(!hasSnapshot){
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>{ensureOverlay();progress(8,'BOOTSTRAP')},{once:true});
  else{ensureOverlay();progress(8,'BOOTSTRAP')}
}

window.fetch=async function(input,init){
  const method=String(init?.method||(typeof input!=='string'&&input?.method)||'GET').toUpperCase();
  let path='';
  try{path=new URL(typeof input==='string'?input:input.url,location.href).pathname}catch{}
  const key=method==='GET'?targetFor(path):null;
  if(!key)return originalFetch(input,init);

  if(hasSnapshot&&!served.has(key)){
    served.add(key);
    if(served.size===2)queueBackgroundRefresh();
    return new Response(JSON.stringify(snapshot[key]),{status:200,headers:{'Content-Type':'application/json','X-Apocalypse-Cache':'snapshot'}});
  }

  if(!hasSnapshot)progress(fresh.world||fresh.ops?58:24,key==='world'?'LOADING WORLD':'LOADING OPS');
  try{
    const response=await originalFetch(input,init);
    if(response.ok){
      try{
        const data=await response.clone().json();
        fresh[key]=data;
        if(!hasSnapshot){
          const n=(fresh.world?1:0)+(fresh.ops?1:0);
          progress(n===1?62:90,n===1?'LOCAL STATE RECEIVED':'RENDERING INTERFACE');
        }
        if(fresh.world&&fresh.ops){saveFresh();if(document.documentElement.dataset.apocalypseBoot!=='cached')setTimeout(finish,30)}
      }catch(e){console.warn('Apocalypse boot response could not be cached',e)}
    }
    return response;
  }catch(e){fail('CHECK THE LOCAL APOCALYPSE SERVICE · '+String(e?.message||e));throw e}
};

window.ApocalypseBootCache={
  hasSnapshot:()=>hasSnapshot,
  savedAt:()=>snapshot?.saved_at||null,
  clear:()=>{try{localStorage.removeItem(CACHE_KEY)}catch{};snapshot=null;hasSnapshot=false;served.clear();fresh={world:null,ops:null}}
};
})();
