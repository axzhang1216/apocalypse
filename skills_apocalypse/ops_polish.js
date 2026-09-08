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
    // The existing focus panel remains open, so the user returns to the enlarged
    // 10×7 activity field rather than falling back to the tiny dashboard tile.
  },true);
})();
