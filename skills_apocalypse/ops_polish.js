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
  const quotaRows=document.getElementById('quotaRows');
  if(!quotaRows)return;

  const style=document.createElement('style');
  style.textContent=`
    .grok-diag-btn{margin:5px 0 2px;padding:3px 7px;border:1px solid rgba(111,148,184,.24);border-radius:3px;background:rgba(111,148,184,.045);color:#91b3d2;font:600 calc(8px * var(--apoc-dynamic-scale)) Consolas,monospace;letter-spacing:.08em;cursor:pointer}.grok-diag-btn:hover{border-color:rgba(111,148,184,.48);background:rgba(111,148,184,.09)}.grok-diag-btn:disabled{opacity:.45;cursor:default}.grok-diag{display:none;margin:5px 0 7px;padding:7px 8px;border-left:1px solid rgba(111,148,184,.25);background:rgba(8,12,17,.32);font:600 calc(7.5px * var(--apoc-dynamic-scale)) Consolas,monospace;line-height:1.55;color:#aab5bf}.grok-diag.open{display:block}.grok-diag-row{display:grid;grid-template-columns:112px 1fr;gap:8px}.grok-diag-row b{font-weight:600;color:#697985}.grok-diag-row span{color:#c4cbd1;overflow-wrap:anywhere}.grok-diag-row.hot span{color:#d8b63f}.grok-diag-row.ok span{color:#8fb0cc}.grok-diag-copy{margin-top:7px;border:0;background:none;padding:0;color:#778fa5;font:600 7px Consolas,monospace;letter-spacing:.08em;cursor:pointer}`;
  document.head.appendChild(style);

  const ordered=[
    ['GROK HOME','grok_home'],['AUTH FILE','auth_file_exists'],['AUTH STATUS','auth_status'],
    ['ISSUER','selected_issuer'],['USER ID','user_id_present'],['TEAM ID','team_id_present'],
    ['EXPIRES','expires_at'],['TOKEN FRESH','token_fresh'],['BILLING HOST','billing_host'],
    ['BILLING HTTP','billing_http'],['PLAN','subscription_tier'],['PERIOD','period_type'],
    ['WEEKLY FIELD','weekly_field_present'],['WEEKLY AVAILABLE','weekly_percent_available'],
    ['WEEKLY USED','weekly_used_percent'],['MONTHLY BUDGET','monthly_budget_present'],['CONCLUSION','conclusion']
  ];
  function shown(k,v){
    if(v===null||v===undefined||v==='')return '—';
    if(typeof v==='boolean')return v?'YES':'NO';
    if(k==='weekly_used_percent')return `${v}%`;
    return String(v);
  }
  function diagText(d){return ordered.map(([label,key])=>`${label}: ${shown(key,d[key])}`).join('\n')}
  function render(panel,d){
    panel.innerHTML='';
    for(const [label,key] of ordered){
      const row=document.createElement('div');
      const value=shown(key,d[key]);
      row.className='grok-diag-row'+(key==='conclusion'?' hot':(key==='billing_http'&&Number(d[key])===200?' ok':''));
      const b=document.createElement('b'),span=document.createElement('span');b.textContent=label;span.textContent=value;row.append(b,span);panel.appendChild(row);
    }
    const copy=document.createElement('button');copy.className='grok-diag-copy';copy.textContent='COPY DIAGNOSTIC';
    copy.onclick=async e=>{e.stopPropagation();try{await navigator.clipboard.writeText(diagText(d));copy.textContent='COPIED'}catch{copy.textContent='COPY FAILED'}};
    panel.appendChild(copy);panel.classList.add('open');
  }
  function ensure(){
    for(const card of quotaRows.querySelectorAll('.qprovider')){
      const name=(card.querySelector('.qhead span')?.textContent||'').trim().toUpperCase();
      if(name!=='GROK'||card.querySelector('.grok-diag-btn'))continue;
      const btn=document.createElement('button');btn.className='grok-diag-btn';btn.textContent='DIAGNOSE';
      const panel=document.createElement('div');panel.className='grok-diag';
      const head=card.querySelector('.qhead');(head||card.firstChild)?.after(btn,panel);
      btn.onclick=async e=>{
        e.stopPropagation();
        if(panel.classList.contains('open')){panel.classList.remove('open');return}
        btn.disabled=true;btn.textContent='DIAGNOSING…';panel.classList.add('open');panel.textContent='READING LOCAL GROK SESSION · CHECKING BILLING…';
        try{
          const r=await fetch('/api/quotas/grok/diagnose',{method:'POST',cache:'no-store'}),d=await r.json();
          if(!r.ok||d.ok===false)throw new Error(d.error||`HTTP ${r.status}`);
          render(panel,d);btn.textContent='DIAGNOSE';
        }catch(err){panel.textContent='DIAGNOSTIC FAILED · '+err.message;btn.textContent='RETRY DIAGNOSE'}
        finally{btn.disabled=false}
      };
    }
  }
  new MutationObserver(ensure).observe(quotaRows,{childList:true,subtree:true});
  ensure();
})();
