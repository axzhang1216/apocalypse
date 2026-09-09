(()=>{
  const SCALE_KEY='apocalypse.uiScale.v2';
  const root=document.documentElement;
  function scale(){const v=Number(localStorage.getItem(SCALE_KEY)||1.15);root.style.setProperty('--apoc-dynamic-scale',String(v));}
  scale();
  document.addEventListener('click',e=>{if(e.target.closest('[data-ui-scale]'))setTimeout(scale,0)},true);

  const grid=document.getElementById('activityGrid');
  function trimActivity(){
    if(!grid)return;
    const days=[...grid.querySelectorAll('.day')];
    if(days.length<=70)return;
    days.slice(0,days.length-70).forEach(d=>d.remove());
  }
  if(grid){new MutationObserver(trimActivity).observe(grid,{childList:true});trimActivity()}

  const zone=document.querySelector('.activity-zone');
  const close=document.getElementById('closeDayLog');
  close?.addEventListener('click',()=>{
    zone?.classList.remove('day-open');
    // Keep Activity focused so another day can be opened without shrinking the panel.
  },true);

  // Staged desktop updater. spatial_os.html owns the base Settings menu; this
  // layer replaces only its old synchronous "download + launch installer" action.
  const updateBtn=document.getElementById('updateBtn');
  const updateMeta=document.getElementById('updateMeta');
  const settingsBtn=document.getElementById('settingsBtn');
  const statusEl=document.getElementById('settingsStatus');
  let updatePoll=null,lastUpdateState=null;

  function setUpdateStatus(text,kind=''){
    if(!statusEl)return;
    statusEl.className='settings-status'+(kind?' '+kind:'');
    statusEl.textContent=text||'';
  }
  function mb(n){return (Number(n||0)/1048576).toFixed(1)}

  if(updateBtn){
    // Remove the legacy onclick assigned by spatial_os.html.
    updateBtn.onclick=null;
    const progress=document.createElement('div');
    progress.className='update-progress';
    progress.innerHTML='<i></i>';
    updateBtn.insertAdjacentElement('afterend',progress);
    const progressBar=progress.querySelector('i');

    const restartBtn=document.createElement('button');
    restartBtn.className='settings-action update-restart';
    restartBtn.innerHTML='<span>RESTART NOW</span><small>APPLY UPDATE</small>';
    progress.insertAdjacentElement('afterend',restartBtn);

    function render(s){
      if(!s)return;
      lastUpdateState=s;
      const phase=s.phase||'idle',target=s.target_version||s.latest_version;
      updateBtn.classList.toggle('busy',phase==='checking'||phase==='downloading');
      progress.classList.toggle('show',phase==='downloading'||phase==='ready');
      restartBtn.classList.toggle('show',phase==='ready'&&!!s.restart_available);
      const pct=Math.max(0,Math.min(100,Math.round(Number(s.progress||0)*100)));
      progressBar.style.width=(phase==='ready'?100:pct)+'%';

      if(phase==='checking'){
        updateMeta.textContent='CHECKING…';
        setUpdateStatus('CHECKING LATEST RELEASE…');
      }else if(phase==='downloading'){
        updateMeta.textContent=`DOWNLOADING ${pct}%`;
        setUpdateStatus(`V${target||'—'} · ${mb(s.downloaded)} / ${mb(s.total)} MB`);
      }else if(phase==='ready'){
        updateMeta.textContent=`READY V${target||'—'}`;
        setUpdateStatus(`READY · V${target} · CLOSE APOCALYPSE TO INSTALL`,'ok');
      }else if(phase==='install_on_exit'||phase==='restarting'){
        updateMeta.textContent='INSTALLING ON EXIT';
        setUpdateStatus('UPDATE ARMED · WAITING FOR APOCALYPSE TO EXIT','ok');
      }else if(phase==='up_to_date'){
        updateMeta.textContent=`V${s.latest_version||s.current_version||'—'} · CURRENT`;
        setUpdateStatus(`UP TO DATE · V${s.current_version||'—'}`,'ok');
      }else if(phase==='available'){
        updateMeta.textContent=`V${target||'—'} AVAILABLE`;
        setUpdateStatus(`UPDATE AVAILABLE · V${target}`);
      }else if(phase==='error'){
        updateMeta.textContent='RETRY UPDATE';
        setUpdateStatus('UPDATE FAILED · '+(s.error||'unknown error'),'err');
      }else{
        updateMeta.textContent='CHECK / DOWNLOAD';
      }
    }

    async function fetchState(){
      try{
        const r=await fetch('/api/settings/update',{cache:'no-store'}),s=await r.json();
        if(!r.ok||s.ok===false)throw new Error(s.error||`HTTP ${r.status}`);
        render(s);return s;
      }catch(err){
        setUpdateStatus('UPDATE CHECK FAILED · '+err.message,'err');
        return null;
      }
    }

    function poll(){
      clearInterval(updatePoll);
      updatePoll=setInterval(async()=>{
        const s=await fetchState();
        if(!s)return;
        if(!['checking','downloading'].includes(s.phase)){clearInterval(updatePoll);updatePoll=null}
      },550);
    }

    updateBtn.addEventListener('click',async e=>{
      e.preventDefault();e.stopPropagation();
      if(['checking','downloading'].includes(lastUpdateState?.phase))return;
      if(lastUpdateState?.phase==='ready'){
        setUpdateStatus(`READY · V${lastUpdateState.target_version} · CLOSE OR USE RESTART NOW`,'ok');
        return;
      }
      updateBtn.classList.add('busy');
      setUpdateStatus('STARTING BACKGROUND UPDATE…');
      try{
        const r=await fetch('/api/settings/update',{method:'POST'}),s=await r.json();
        if(!r.ok||s.ok===false)throw new Error(s.error||`HTTP ${r.status}`);
        render(s);poll();
      }catch(err){
        setUpdateStatus('UPDATE FAILED · '+err.message,'err');updateBtn.classList.remove('busy');
      }
    },true);

    restartBtn.addEventListener('click',async e=>{
      e.preventDefault();e.stopPropagation();
      restartBtn.classList.add('busy');
      setUpdateStatus('RESTARTING · UPDATE WILL INSTALL SILENTLY…','ok');
      try{
        const r=await fetch('/api/settings/update',{method:'POST'}),s=await r.json();
        if(!r.ok||s.ok===false)throw new Error(s.error||`HTTP ${r.status}`);
        updateMeta.textContent='RESTARTING…';
      }catch(err){
        restartBtn.classList.remove('busy');setUpdateStatus('RESTART FAILED · '+err.message,'err');
      }
    },true);

    // Opening Settings restores a persisted staged update after app/UI reloads.
    settingsBtn?.addEventListener('click',()=>setTimeout(fetchState,80));
  }
})();

(()=>{
  // Fast boot snapshot. The canonical refresh still runs exactly as before;
  // this only paints the last successful WORLD + OPS while that local refresh
  // is being computed. A fresh pair atomically replaces the snapshot.
  const KEY='apocalypse.bootSnapshot.v1',SCHEMA=1;
  let cached=null,cachedWorld=null,cachedOps=null,overlay=null,bar=null,label=null,pct=null,note=null;
  let restored=false,done=false,started=performance.now(),previewTimer=null;

  function safeState(){
    try{return {world:typeof WORLD!=='undefined'?WORLD:null,ops:typeof OPS!=='undefined'?OPS:null}}catch{return {world:null,ops:null}}
  }
  function read(){
    try{
      const x=JSON.parse(localStorage.getItem(KEY)||'null');
      if(x&&x.schema===SCHEMA&&x.world&&x.ops)return x;
    }catch(e){console.warn('Apocalypse boot cache unreadable',e)}
    return null;
  }
  function compactWorld(w){
    // Discussion messages can be fetched on demand and are often the largest
    // part of WORLD. Excluding them keeps the persistent snapshot small.
    try{return {...w,objects:(w.objects||[]).map(o=>o?.type==='decision'&&o.messages?{...o,messages:[]}:o)}}catch{return w}
  }
  function write(w,o){
    try{localStorage.setItem(KEY,JSON.stringify({schema:SCHEMA,saved_at:Date.now(),world:compactWorld(w),ops:o}))}
    catch(e){console.warn('Apocalypse boot cache write failed',e)}
  }
  function makeOverlay(){
    if(overlay||restored)return;
    const style=document.createElement('style');style.textContent=`
      .apoc-boot{position:fixed;inset:0;z-index:180;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 50% 46%,rgba(111,148,184,.07),transparent 28%),rgba(15,19,25,.18);opacity:1;transition:opacity .42s ease;pointer-events:none}.apoc-boot.done{opacity:0}.apoc-boot.error{pointer-events:auto}.apoc-boot-card{width:min(430px,72vw);transform:translateY(-2vh);font-family:Consolas,monospace}.apoc-boot-logo{display:flex;align-items:center;gap:9px;margin-bottom:15px;color:#e6e2da;font-size:12px;font-weight:700;letter-spacing:.14em}.apoc-boot-logo i{width:6px;height:6px;border-radius:50%;background:#e48aae;box-shadow:0 0 17px rgba(228,138,174,.85)}.apoc-boot-meta{display:flex;justify-content:space-between;margin-bottom:8px;color:#91a0ad;font-size:9px;font-weight:600;letter-spacing:.11em}.apoc-boot-track{height:2px;background:rgba(255,255,255,.055);overflow:hidden}.apoc-boot-bar{height:100%;width:8%;background:linear-gradient(90deg,#6f94b8,#e48aae);box-shadow:0 0 14px rgba(111,148,184,.35);transition:width .32s cubic-bezier(.16,.84,.32,1)}.apoc-boot-note{margin-top:9px;color:#626e79;font-size:8px;font-weight:600;letter-spacing:.08em}.apoc-boot-retry{display:none;margin-top:14px;padding:7px 10px;border:1px solid rgba(111,148,184,.24);border-radius:4px;background:rgba(111,148,184,.05);color:#b8c3cd;font:9px Consolas,monospace;letter-spacing:.08em;pointer-events:auto}.apoc-boot.error .apoc-boot-retry{display:inline-block}`;document.head.appendChild(style);
    overlay=document.createElement('div');overlay.className='apoc-boot';overlay.innerHTML='<div class="apoc-boot-card"><div class="apoc-boot-logo"><i></i>APOCALYPSE</div><div class="apoc-boot-meta"><span>BOOTSTRAP</span><span>08%</span></div><div class="apoc-boot-track"><div class="apoc-boot-bar"></div></div><div class="apoc-boot-note">LOADING LOCAL WORLD STATE</div><button class="apoc-boot-retry">RETRY</button></div>';document.body.appendChild(overlay);
    bar=overlay.querySelector('.apoc-boot-bar');label=overlay.querySelector('.apoc-boot-meta span');pct=overlay.querySelector('.apoc-boot-meta span:last-child');note=overlay.querySelector('.apoc-boot-note');overlay.querySelector('.apoc-boot-retry').onclick=()=>location.reload();
  }
  function progress(v,text){
    if(restored||done)return;makeOverlay();if(!overlay)return;const n=Math.max(0,Math.min(100,Math.round(v)));bar.style.width=n+'%';pct.textContent=String(n).padStart(2,'0')+'%';if(text)label.textContent=text;
  }
  function finish(){
    if(done)return;done=true;progress(100,'WORLD ONLINE');if(overlay){bar.style.width='100%';pct.textContent='100%';label.textContent='WORLD ONLINE';setTimeout(()=>{overlay.classList.add('done');setTimeout(()=>overlay.remove(),460)},120)}
  }
  function errorState(){
    if(restored||done||!overlay)return;overlay.classList.add('error');label.textContent='LOCAL API UNAVAILABLE';pct.textContent='—';note.textContent='APOCALYPSE COULD NOT LOAD LOCAL STATE';
  }
  function paintCached(){
    cached=read();if(!cached)return false;
    try{
      cachedWorld=cached.world;cachedOps=cached.ops;WORLD=cachedWorld;OPS=cachedOps;prepareLayout();renderOps();setTimeout(()=>{try{fitWeather()}catch{}},35);try{drawStars(performance.now());drawUniverse(performance.now())}catch{}
      restored=true;document.documentElement.dataset.apocalypseBootCache='restored';
      // Four lightweight preview frames per second until the canonical loop is
      // expected to take over. This prevents a static black canvas during a slow scan.
      const until=performance.now()+5000;previewTimer=setInterval(()=>{if(performance.now()>until){clearInterval(previewTimer);previewTimer=null;return}try{drawStars(performance.now());drawUniverse(performance.now())}catch{}},250);
      return true;
    }catch(e){console.warn('Apocalypse cached snapshot could not be rendered',e);restored=false;return false}
  }

  if(!paintCached()){makeOverlay();progress(8,'BOOTSTRAP')}

  const stages=[[300,24,'SCANNING WORKSPACE'],[850,42,'LOADING WORLD'],[1700,61,'LOADING OPS'],[3200,76,'BUILDING INTERFACE'],[6200,87,'WAITING FOR LOCAL API']];
  for(const [delay,value,text] of stages)setTimeout(()=>progress(value,text),delay);

  const poll=setInterval(()=>{
    const s=safeState();
    if(s.world&&s.ops){
      const fresh=!restored||(s.world!==cachedWorld&&s.ops!==cachedOps);
      if(fresh){write(s.world,s.ops);clearInterval(poll);if(previewTimer){clearInterval(previewTimer);previewTimer=null}finish();return}
    }
    if(!restored&&performance.now()-started>12000){
      try{if((document.getElementById('runText')?.textContent||'').includes('API OFFLINE'))errorState();else progress(92,'STILL LOADING') }catch{}
    }
  },140);

  window.ApocalypseBootCache={
    clear(){try{localStorage.removeItem(KEY)}catch{}},
    savedAt(){try{return JSON.parse(localStorage.getItem(KEY)||'null')?.saved_at||null}catch{return null}}
  };
})();
