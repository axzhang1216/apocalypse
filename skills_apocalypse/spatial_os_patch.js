(()=>{
  const SCALE_KEY='apocalypse.uiScale';
  const STEPS=[1,1.15,1.3,1.45];
  const root=document.documentElement;
  const scaleBtn=document.getElementById('scaleBtn');
  const universe=document.getElementById('universe');
  let currentScale=Number(localStorage.getItem(SCALE_KEY)||1.15);
  if(!STEPS.includes(currentScale)) currentScale=1.15;

  const baseline=new WeakMap();
  function scaleElement(el){
    if(!(el instanceof HTMLElement)) return;
    if(el.closest('.ui-scale-menu')) return;
    let base=baseline.get(el);
    if(!base){
      const fs=parseFloat(getComputedStyle(el).fontSize);
      if(!Number.isFinite(fs)||fs<=0) return;
      base=fs;
      baseline.set(el,base);
    }
    el.style.fontSize=(base*currentScale).toFixed(2)+'px';
  }
  function scaleDom(){
    document.querySelectorAll('body *').forEach(scaleElement);
    if(scaleBtn){
      scaleBtn.dataset.scale=Math.round(currentScale*100)+'%';
      scaleBtn.title=`UI scale · ${Math.round(currentScale*100)}%`;
    }
  }
  const mo=new MutationObserver(records=>{
    for(const r of records){
      for(const n of r.addedNodes){
        if(!(n instanceof HTMLElement)) continue;
        scaleElement(n);
        n.querySelectorAll?.('*').forEach(scaleElement);
      }
    }
  });
  mo.observe(document.body,{childList:true,subtree:true});

  // Canvas labels use explicit px fonts in spatial_os.js. Wrap this canvas context
  // so those labels follow the same UI scale without changing node geometry.
  try{
    if(typeof ctx!=='undefined'&&ctx){
      const proto=Object.getPrototypeOf(ctx);
      const desc=Object.getOwnPropertyDescriptor(proto,'font');
      if(desc?.get&&desc?.set){
        Object.defineProperty(ctx,'font',{
          configurable:true,
          get(){ return desc.get.call(ctx); },
          set(v){
            const scaled=String(v).replace(/([0-9]*\.?[0-9]+)px/g,(_,n)=>`${(parseFloat(n)*currentScale).toFixed(2)}px`);
            desc.set.call(ctx,scaled);
          }
        });
      }
    }
  }catch(e){ console.warn('Canvas font scaling unavailable',e); }

  const menu=document.createElement('div');
  menu.className='ui-scale-menu';
  menu.innerHTML=STEPS.map(v=>`<button data-ui-scale="${v}">${Math.round(v*100)}%</button>`).join('');
  document.body.appendChild(menu);

  function setScale(v){
    currentScale=v;
    root.style.setProperty('--apocalypse-ui-scale',v);
    localStorage.setItem(SCALE_KEY,String(v));
    menu.querySelectorAll('button').forEach(b=>b.classList.toggle('active',Number(b.dataset.uiScale)===v));
    scaleDom();
  }
  menu.addEventListener('click',e=>{
    const b=e.target.closest('[data-ui-scale]');
    if(!b) return;
    setScale(Number(b.dataset.uiScale));
    menu.classList.remove('open');
  });
  scaleBtn?.addEventListener('click',e=>{
    e.stopPropagation();
    menu.classList.toggle('open');
  });
  document.addEventListener('click',e=>{
    if(!menu.contains(e.target)&&e.target!==scaleBtn) menu.classList.remove('open');
  });
  setScale(currentScale);

  // SPACE: when a project is focused, clicking empty outer space returns to the
  // project overview. Dragging/panning and clicking nodes keep their old behavior.
  let down=null;
  const hint=document.createElement('div');
  hint.className='space-back-hint';
  hint.textContent='CLICK OUTSIDE · ALL PROJECTS';
  document.body.appendChild(hint);

  universe?.addEventListener('pointerdown',e=>{ down={x:e.clientX,y:e.clientY}; },true);
  universe?.addEventListener('pointermove',e=>{
    try{
      if(typeof focus!=='undefined'&&focus&&typeof hit==='function'){
        const empty=!hit(e.clientX,e.clientY);
        hint.classList.toggle('show',empty&&!down);
        universe.style.cursor=empty?'zoom-out':'';
      }else{
        hint.classList.remove('show');
        universe.style.cursor='';
      }
    }catch{}
  });
  universe?.addEventListener('pointerleave',()=>{
    hint.classList.remove('show');
    universe.style.cursor='';
  });
  universe?.addEventListener('pointerup',e=>{
    const start=down; down=null;
    if(!start) return;
    if(Math.hypot(e.clientX-start.x,e.clientY-start.y)>5) return;
    try{
      if(typeof focus==='undefined'||!focus||typeof hit!=='function'||typeof clearFocus!=='function') return;
      if(hit(e.clientX,e.clientY)) return;
      clearFocus();
      hint.classList.remove('show');
      universe.style.cursor='';
    }catch(err){ console.warn('Outer-space back navigation unavailable',err); }
  },false);
})();
