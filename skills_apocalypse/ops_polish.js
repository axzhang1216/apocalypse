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
  const daySummary=document.getElementById('daySummary');
  const dayStats=document.getElementById('dayStats');
  const dayWork=document.getElementById('dayWork');
  const dayDate=document.getElementById('dayLogDate');
  function restoreActivityGrid(){
    if(!zone)return;
    zone.classList.remove('day-open');
    if(daySummary)daySummary.textContent='';
    if(dayStats)dayStats.innerHTML='';
    if(dayWork)dayWork.innerHTML='';
    if(dayDate)dayDate.textContent='DAY LOG';
  }
  close?.addEventListener('click',()=>{
    restoreActivityGrid();
  },true);
  if(zone){
    new MutationObserver(()=>{
      if(!zone.classList.contains('focus')&&zone.classList.contains('day-open'))restoreActivityGrid();
    }).observe(zone,{attributes:true,attributeFilter:['class']});
  }
  document.addEventListener('click',e=>{
    if(e.target.closest('#opsFocusVeil')&&zone?.classList.contains('day-open'))restoreActivityGrid();
  },true);
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape'&&zone?.classList.contains('day-open'))restoreActivityGrid();
  },true);

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

    settingsBtn?.addEventListener('click',()=>setTimeout(fetchState,80));
  }
})();

(()=>{
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


(()=>{
  // One-way local AI-agent chat archive. It never writes back to agent stores.
  const menu=document.querySelector('.settings-menu');
  const settingsBtn=document.getElementById('settingsBtn');
  const globalStatus=document.getElementById('settingsStatus');
  if(!menu||!globalStatus)return;

  const section=document.createElement('div');
  section.className='settings-section archive-settings';
  section.innerHTML=
    '<div class="settings-label"><span>STORAGE</span><b id="archiveState">AUTO SYNC</b></div>'+
    '<div class="archive-path-row">'+
      '<input id="archivePath" class="archive-path" type="text" spellcheck="false" autocomplete="off" placeholder="Archive folder">'+
      '<button id="archiveBrowse" class="archive-mini">BROWSE</button>'+
    '</div>'+
    '<div class="archive-actions">'+
      '<button id="archiveSave" class="archive-mini primary">SAVE</button>'+
      '<button id="archiveSync" class="archive-mini">SYNC NOW</button>'+
    '</div>'+
    '<div class="archive-meta" id="archiveMeta">LOCAL AGENT CHAT ARCHIVE · ONE-WAY BACKUP</div>'+
    '<div class="archive-agents" id="archiveAgents"></div>';
  globalStatus.insertAdjacentElement('beforebegin',section);

  const pathInput=section.querySelector('#archivePath');
  const browseBtn=section.querySelector('#archiveBrowse');
  const saveBtn=section.querySelector('#archiveSave');
  const syncBtn=section.querySelector('#archiveSync');
  const stateEl=section.querySelector('#archiveState');
  const metaEl=section.querySelector('#archiveMeta');
  const agentsEl=section.querySelector('#archiveAgents');
  let current=null,poll=null;

  function shortTime(value){
    if(!value)return 'NOT YET';
    const d=new Date(value);
    if(Number.isNaN(d.getTime()))return 'UNKNOWN';
    return d.toLocaleString([], {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
  }
  function render(s){
    if(!s)return;
    current=s;
    if(document.activeElement!==pathInput)pathInput.value=s.root||'';
    stateEl.textContent=s.running?'SYNCING…':'AUTO · '+Math.max(1,Math.round(Number(s.interval_seconds||300)/60))+' MIN';
    stateEl.classList.toggle('live',!!s.running);
    const copied=Number(s.copied_files||0),dbs=Number(s.exported_databases||0);
    metaEl.textContent=s.last_error
      ? 'LAST '+shortTime(s.last_sync_at)+' · '+s.last_error
      : 'LAST '+shortTime(s.last_sync_at)+' · '+copied+' FILES · '+dbs+' DB EXPORTS';
    metaEl.classList.toggle('err',!!s.last_error);
    const entries=Object.entries(s.agents||{});
    agentsEl.innerHTML=entries.length
      ? entries.map(function(pair){const name=pair[0],v=pair[1]||{};return '<span><b>'+name.toUpperCase()+'</b> '+(Number(v.copied||0)+Number(v.unchanged||0)+Number(v.databases||0))+'</span>'}).join('')
      : '<span>WAITING FOR FIRST SYNC</span>';
    syncBtn.disabled=!!s.running;
    syncBtn.textContent=s.running?'SYNCING…':'SYNC NOW';
  }
  async function load(){
    try{
      const r=await fetch('/api/storage/status',{cache:'no-store'}),j=await r.json();
      if(!r.ok||j.ok===false)throw new Error(j.error||('HTTP '+r.status));
      render(j);return j;
    }catch(err){
      metaEl.textContent='STORAGE STATUS FAILED · '+err.message;metaEl.classList.add('err');return null;
    }
  }
  async function save(){
    const root=pathInput.value.trim();
    if(!root){metaEl.textContent='CHOOSE A STORAGE FOLDER';metaEl.classList.add('err');return}
    saveBtn.disabled=true;saveBtn.textContent='SAVING…';
    try{
      const r=await fetch('/api/storage/config',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({root:root})}),j=await r.json();
      if(!r.ok||j.ok===false)throw new Error(j.error||('HTTP '+r.status));
      render(j);metaEl.textContent='STORAGE SAVED · BACKGROUND SYNC QUEUED';metaEl.classList.remove('err');
    }catch(err){metaEl.textContent='SAVE FAILED · '+err.message;metaEl.classList.add('err')}
    finally{saveBtn.disabled=false;saveBtn.textContent='SAVE'}
  }

  browseBtn.addEventListener('click',async function(e){
    e.preventDefault();e.stopPropagation();
    browseBtn.disabled=true;browseBtn.textContent='OPENING…';
    try{
      const initial=pathInput.value||(current&&current.root)||'';
      let j=null;
      const api=window.pywebview&&window.pywebview.api;
      const picker=api&&api.select_storage_folder;
      if(picker){
        j=await picker(initial);
      }else{
        const r=await fetch('/api/storage/browse',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({initial:initial})});
        j=await r.json();
        if(!r.ok||j.ok===false)throw new Error(j.error||('HTTP '+r.status));
      }
      if(j&&j.ok&&j.path){pathInput.value=j.path;await save()}
      else if(j&&j.cancelled){metaEl.textContent='FOLDER PICKER CANCELLED';metaEl.classList.remove('err')}
      else if(j&&j.error){throw new Error(j.error)}
      else{metaEl.textContent='NO FOLDER SELECTED';metaEl.classList.add('err')}
    }catch(err){metaEl.textContent='FOLDER PICKER FAILED · '+err.message;metaEl.classList.add('err')}
    finally{browseBtn.disabled=false;browseBtn.textContent='BROWSE'}
  });
  saveBtn.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();save()});
  syncBtn.addEventListener('click',async function(e){
    e.preventDefault();e.stopPropagation();syncBtn.disabled=true;syncBtn.textContent='QUEUED…';
    try{
      const r=await fetch('/api/storage/sync',{method:'POST'}),j=await r.json();
      if(!r.ok||j.ok===false)throw new Error(j.error||('HTTP '+r.status));
      render(j);clearInterval(poll);poll=setInterval(async function(){const s=await load();if(s&&!s.running){clearInterval(poll);poll=null}},650);
    }catch(err){metaEl.textContent='SYNC FAILED · '+err.message;metaEl.classList.add('err');syncBtn.disabled=false;syncBtn.textContent='SYNC NOW'}
  });
  settingsBtn&&settingsBtn.addEventListener('click',function(){setTimeout(load,90)});
  load();
})();
