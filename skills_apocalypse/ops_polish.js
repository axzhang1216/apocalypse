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
