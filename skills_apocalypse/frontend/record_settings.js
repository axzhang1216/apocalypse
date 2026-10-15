(()=>{
  const menu=document.getElementById('settingsMenu');
  const statusEl=document.getElementById('settingsStatus');
  if(!menu||document.getElementById('projectRecordSettings'))return;
  const section=document.createElement('div');
  section.className='settings-section';
  section.id='projectRecordSettings';
  section.innerHTML=`
    <div class="settings-label"><span>PROJECT CHAT RECORD</span><b id="recordState">LOADING</b></div>
    <button class="settings-action" id="recordToggle" type="button">
      <span>为项目文件夹开启聊天记录监控</span><small id="recordToggleMeta">DEFAULT ON</small>
    </button>
    <div id="recordHealth" style="padding:9px 4px 1px;color:var(--mute);font:10px/1.55 Consolas,monospace"></div>`;
  menu.insertBefore(section,statusEl||null);
  const state=document.getElementById('recordState');
  const toggle=document.getElementById('recordToggle');
  const meta=document.getElementById('recordToggleMeta');
  const health=document.getElementById('recordHealth');
  let current=true;
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const render=r=>{
    current=!!r.enabled;
    const bad=!!r.last_error;
    state.textContent=!current?'OFF':bad?'ERROR':r.running?'ACTIVE':'STARTING';
    state.style.color=!current?'var(--mute)':bad?'var(--pink)':'var(--cyan)';
    meta.textContent=current?'ON · CLICK TO DISABLE':'OFF · CLICK TO ENABLE';
    const sync=r.last_sync_at?new Date(r.last_sync_at).toLocaleString():'not yet';
    health.innerHTML=`${esc(r.record_name||'chat_history/record.md')} · NO LLM<br>`+
      `last sync ${esc(sync)} · ${Number(r.monitored_projects||0)} projects · ${Number(r.tracked_sessions||0)} sessions · ${Number(r.messages_written||0)} messages`+
      (bad?`<br><span style="color:var(--pink)">${esc(r.last_error)}</span>`:'');
  };
  const refresh=async()=>{
    try{
      const r=await fetch('/api/settings/project-records',{cache:'no-store'}).then(x=>x.json());
      render(r);
    }catch(e){state.textContent='ERROR';health.textContent='record monitor status unavailable';}
  };
  toggle.addEventListener('click',async()=>{
    toggle.disabled=true;
    try{
      const res=await fetch('/api/settings/project-records',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!current})});
      const r=await res.json();
      if(!res.ok||r.ok===false)throw new Error(r.error||`HTTP ${res.status}`);
      render(r);
    }catch(e){if(statusEl){statusEl.className='settings-status err';statusEl.textContent='CHAT RECORD SETTING FAILED · '+e.message;}}
    finally{toggle.disabled=false;}
  });
  document.getElementById('settingsBtn')?.addEventListener('click',()=>setTimeout(refresh,0));
  refresh();
})();
