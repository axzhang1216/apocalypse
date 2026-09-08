#!/usr/bin/env python3
"""Spatial OS bridge for Apocalypse.

Keeps skills_apocalypse/server.py as the source of truth for Claude/Codex
sessions, transcripts, launch/export/delete, workspace update, and legacy SSE.
Adds the new SPACE/OPS frontend plus aggregation endpoints.
"""
from __future__ import annotations
import hashlib,http.server,json,math,os,queue,sys,threading,time
from collections import Counter,defaultdict
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import parse_qs,urlparse
sys.path.insert(0,str(Path(__file__).resolve().parent))
import server as legacy
PORT=legacy.PORT;BASE=Path(__file__).resolve().parent
SPATIAL_HTML=BASE/'spatial_os.html';SPATIAL_CSS=BASE/'spatial_os.css';SPATIAL_JS=BASE/'spatial_os.js'
QUOTA_FILE=legacy.DATA_DIR/'quotas.json';SCHEDULE_FILE=legacy.DATA_DIR/'schedule.json'
DIMS=('science','ai','modeling','agent','infra','design','data','writing')
HINTS={'science':('climate','ozone','wrf','atmos','chem','air','research','paper','气候','臭氧','大气','化学','论文'),'ai':('ai','llm','model','machine learning','diffusion','cnn','agent','claude','codex','grok','人工智能','机器学习'),'modeling':('wrf','simulation','forecast','model','physics','chemistry','模拟','预报','参数化'),'agent':('agent','claude','codex','pi','openclaw','skill','mcp','智能体'),'infra':('server','ssh','proxy','api','cloudflare','netlify','terminal','deploy','linux','windows','服务器','部署','代理'),'design':('ui','ux','frontend','website','dashboard','workspace','3d','visual','design','界面','网页','可视化'),'data':('data','taxonomy','analysis','csv','json','metric','analytics','数据','分析','指标'),'writing':('paper','latex','review','rebuttal','statement','caption','readout','doc','论文','写作','文档')}
def dt(ts):
    if not ts:return None
    try:return datetime.fromisoformat(str(ts).replace('Z','+00:00')).astimezone(timezone.utc)
    except:return None
def iso(x):return x.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
def sid(prefix,value):return prefix+'-'+hashlib.sha1(value.encode('utf-8',errors='replace')).hexdigest()[:12]
def semantic(*parts):
    text=' '.join(str(p or '') for p in parts).lower();vals=[]
    for d in DIMS:
        v=.04+sum(.34 for t in HINTS[d] if t in text);vals.append(min(1.,v))
    seed=int(hashlib.sha1(text.encode()).hexdigest()[:8],16)
    return[min(1,v+(((seed>>(i*3))&7)/7)*.045) for i,v in enumerate(vals)]
def cosine(a,b):
    ab=sum(x*y for x,y in zip(a,b));aa=math.sqrt(sum(x*x for x in a));bb=math.sqrt(sum(x*x for x in b));return ab/(aa*bb or 1)
def live_map():return{s.get('session_id'):s for s in legacy.scan_transcripts(limit=None)}
def world():
    ws=legacy._load_workspace() or {};groups=defaultdict(list);live=live_map();objects=[];edges=[];projects=[]
    for key,p in (ws.get('projects') or {}).items():groups[(p.get('title') or p.get('name') or Path(str(key)).name or 'Unknown').strip()].append((key,p))
    for title,members in groups.items():
        pid=sid('p',title);tags=[];sessions=[];points=[];last='';cwds=[]
        for key,p in members:
            cwds.append(p.get('cwd') or str(key));last=max(last,p.get('last_active') or '')
            for t in p.get('tags') or []:
                if t not in tags:tags.append(t)
            sessions.extend((k,v,p) for k,v in (p.get('analyzed_sessions') or {}).items())
            points.extend(dict(x) for x in p.get('points') or [])
        sessions.sort(key=lambda r:r[1].get('ts') or '',reverse=True);goal=next((m.get('user_goal') for _,m,_ in sessions if m.get('user_goal')),'');summary=next((m.get('summary') for _,m,_ in sessions if m.get('summary')),'')
        status='active' if any((live.get(x[0]) or {}).get('status')=='green' for x in sessions) else 'waiting' if any((live.get(x[0]) or {}).get('status')=='yellow' for x in sessions) else 'idle'
        po={'id':pid,'type':'project','name':title,'title':title,'tags':tags,'goal':goal or summary or 'Workspace project','summary':summary,'sessions':len(sessions),'decisions':len(points),'status':status,'mass':min(1,.28+math.log2(1+len(sessions))*.13),'importance':min(1,.4+len(sessions)/40),'last_active':last,'cwd':cwds[0] if cwds else '', 'cwds':cwds,'semantic':semantic(title,tags,goal,summary),'color':'#6F94B8'};objects.append(po);projects.append(po)
        for s,meta,p in sessions:
            sm=live.get(s) or {};state={'green':'active','yellow':'waiting','grey':'idle'}.get(sm.get('status'),'idle');out=meta.get('outcome') or 'partial'
            objects.append({'id':sid('s',s),'type':'session','title':meta.get('user_goal') or meta.get('summary') or s[:8],'name':meta.get('user_goal') or meta.get('summary') or s[:8],'project_id':pid,'session_id':s,'provider':'claude','state':state,'goal':meta.get('user_goal') or '','summary':meta.get('summary') or '','outcome':out,'category':meta.get('category') or 'other','ts':meta.get('ts') or sm.get('last_ts') or '','cwd':p.get('cwd') or sm.get('cwd') or '','resume':s,'importance':{'completed':.82,'partial':.64,'abandoned':.42}.get(out,.58)})
        ids={};
        for i,x in enumerate(points):
            raw=str(x.get('id') or f"{x.get('session_id','')}:{i}:{x.get('topic','')}");ids[raw]=sid('d',pid+':'+raw)
        for i,x in enumerate(points):
            raw=str(x.get('id') or f"{x.get('session_id','')}:{i}:{x.get('topic','')}");objects.append({'id':ids[raw],'type':'decision','title':x.get('topic') or 'Discussion / decision','name':x.get('topic') or 'Discussion / decision','project_id':pid,'session_id':x.get('session_id') or '','topic':x.get('topic') or '','decision':x.get('decision') or '','related_to':[ids[str(r)] for r in x.get('related_to') or [] if str(r) in ids],'messages':x.get('messages') or [],'ts':x.get('ts') or '','state':'recorded','importance':.78 if x.get('decision') else .58})
    for i,a in enumerate(projects):
        for b in projects[i+1:]:
            shared=len(set(a['tags'])&set(b['tags']));w=min(1,cosine(a['semantic'],b['semantic'])*.86+min(shared,3)*.08)
            if w>=.46:edges.append({'source':a['id'],'target':b['id'],'weight':round(w,3)})
    return{'generated_at':iso(datetime.now(timezone.utc)),'objects':objects,'edges':edges}
def iter_activity():
    if not legacy.PROJECTS_DIR.exists():return
    for pd in legacy.PROJECTS_DIR.iterdir():
        if not pd.is_dir():continue
        for p in pd.glob('*.jsonl'):
            first=last=prev=None;active=0.;tools=msgs=0;cwd=''
            try:
                with open(p,'r',encoding='utf-8',errors='replace') as f:
                    for raw in f:
                        try:r=json.loads(raw)
                        except:continue
                        if not cwd and r.get('cwd'):cwd=r.get('cwd') or ''
                        if r.get('type') in legacy.SKIP_TYPES:continue
                        t=dt(r.get('timestamp'))
                        if t:
                            first=first or t;last=t
                            if prev:active+=min(max((t-prev).total_seconds(),0),1200)
                            prev=t
                        if r.get('type') in ('user','assistant'):msgs+=1
                        if r.get('type')=='assistant':
                            c=(r.get('message') or {}).get('content') or []
                            if isinstance(c,list):tools+=sum(1 for x in c if isinstance(x,dict) and x.get('type')=='tool_use')
            except:continue
            if first is None:
                try:first=last=datetime.fromtimestamp(p.stat().st_mtime,tz=timezone.utc)
                except:continue
            if active<=0 and msgs:active=min(msgs*75,720)
            yield{'session_id':p.stem,'project':legacy._project_name(cwd) if cwd else pd.name,'cwd':cwd,'first':first,'last':last or first,'active':active,'tools':tools,'messages':msgs}
def activity(days=84):
    now=datetime.now(timezone.utc);start=(now-timedelta(days=days-1)).date();b={start+timedelta(days=i):{'active':0.,'sessions':0,'tools':0} for i in range(days)}
    for r in iter_activity() or []:
        d=r['last'].date()
        if d in b:b[d]['active']+=r['active'];b[d]['sessions']+=1;b[d]['tools']+=r['tools']
    rows=[{'date':d.isoformat(),'active_hours':round(v['active']/3600,2),'sessions':v['sessions'],'tools':v['tools']} for d,v in b.items()];return{'window_days':days,'active_hours':round(sum(x['active_hours'] for x in rows),1),'days':rows}
def ws_lookup():
    ws=legacy._load_workspace() or {};lookup={};points=Counter()
    for p in (ws.get('projects') or {}).values():
        title=p.get('title') or p.get('name') or 'Unknown'
        for x in p.get('points') or []:
            if x.get('session_id'):points[x['session_id']]+=1
        for s,m in (p.get('analyzed_sessions') or {}).items():lookup[s]={**m,'project_title':title,'cwd':p.get('cwd') or ''}
    return lookup,points
def day_payload(text):
    try:target=datetime.strptime(text,'%Y-%m-%d').date()
    except:target=datetime.now(timezone.utc).date()
    lookup,pc=ws_lookup();rows=[];active=tools=sessions=decisions=0
    for r in iter_activity() or []:
        if r['last'].date()!=target:continue
        sessions+=1;active+=r['active'];tools+=r['tools'];decisions+=pc.get(r['session_id'],0);m=lookup.get(r['session_id'],{});rows.append({'time':r['last'].astimezone().strftime('%H:%M'),'project':m.get('project_title') or r['project'],'title':m.get('summary') or m.get('user_goal') or 'Claude session '+r['session_id'][:8],'detail':m.get('user_goal') or f"{r['messages']} messages · {r['tools']} tool calls",'source':['session'],'artifacts':[],'session_id':r['session_id']})
    rows.sort(key=lambda x:x['time']);names=[]
    for x in rows:
        if x['project'] not in names:names.append(x['project'])
    return{'date':target.isoformat(),'summary':(f"{len(rows)} sessions across {', '.join(names[:3])}." if rows else 'No recorded coding activity for this day.'),'stats':{'active_hours':round(active/3600,2),'sessions':sessions,'tools':tools,'decisions':decisions},'completed_work':rows}
def adapter(path,fallback):
    try:
        if path.exists():return json.loads(path.read_text(encoding='utf-8')),'file'
    except:pass
    return fallback,'demo'
def quotas():
    d=[{'provider':'Claude','status':'demo','five_hour':{'remaining':.76,'reset_in_min':218},'weekly':{'remaining':.63,'reset_in_min':4740}},{'provider':'OpenAI','status':'demo','five_hour':{'remaining':.58,'reset_in_min':154},'weekly':{'remaining':.71,'reset_in_min':6530}},{'provider':'Grok','status':'demo','five_hour':{'remaining':.84,'reset_in_min':264},'weekly':{'remaining':.92,'reset_in_min':7440}}];x,src=adapter(QUOTA_FILE,d);rows=x if isinstance(x,list) else x.get('providers',[])
    for r in rows:r.setdefault('source',src)
    return rows
def schedule():
    d={'events':[{'start':'09:30','end':'10:10','title':'Paper revision','project':'Climate Penalty','hard':True},{'start':'13:30','end':'14:10','title':'Spatial OS integration','project':'Apocalypse','hard':True}],'tasks':[{'title':'Review active session notes','priority':'medium','due':'TODAY','estimate_min':25},{'title':'Refresh workspace classifications','priority':'low','due':'TODAY','estimate_min':15}],'suggested':[]};x,src=adapter(SCHEDULE_FILE,d);x=x if isinstance(x,dict) else d;x.setdefault('events',[]);x.setdefault('tasks',[]);x.setdefault('suggested',[]);x['source']=src;return x
def recent_events(minutes=60):
    cut=datetime.now(timezone.utc)-timedelta(minutes=minutes);return[(dt(e.get('ts')),e) for e in legacy.read_events(5000) if dt(e.get('ts')) and dt(e.get('ts'))>=cut]
def flow():
    ev=recent_events();sessions=legacy.scan_transcripts(limit=None);active=sum(1 for x in sessions if x.get('status')=='green');waiting=sum(1 for x in sessions if x.get('status')=='yellow');tools=sum(1 for _,e in ev if e.get('type') in ('tool_start','tool_end'));load=min(1,.08+active*.18+waiting*.045+min(.55,tools/120));coding=min(.84,.38+tools/max(len(ev),1)*.45);wait=min(.42,waiting/max(active+waiting,1)*.42);reason=max(.08,1-coding-wait);n=reason+coding+wait;return{'window_min':60,'current':{'load':round(load,3),'active_agents':active,'waiting_agents':waiting,'tool_events':tools,'reasoning_ratio':round(reason/n,3),'coding_ratio':round(coding/n,3),'waiting_ratio':round(wait/n,3)}}
def agents(limit=6):
    ev={};tool={}
    for e in legacy.read_events(500):
        s=e.get('session_id')
        if not s:continue
        ev[s]=e
        if e.get('type')=='tool_start':tool[s]=e.get('tool') or ''
    live=[x for x in legacy.scan_transcripts(limit=None) if x.get('status') in ('green','yellow')];live.sort(key=lambda x:x.get('last_ts') or '',reverse=True);rows=[]
    for i,x in enumerate(live[:limit]):
        s=x.get('session_id') or '';state='coding' if x.get('status')=='green' else 'waiting';rows.append({'id':'claude-'+s[:8].lower(),'session_id':s,'name':f'CLAUDE-{i+1:02d}','provider':'claude','project':x.get('project_name') or 'Unknown','task':tool.get(s) or ('waiting for input' if state=='waiting' else 'working'),'status':state,'load':.82 if state=='coding' else .24,'progress':.68 if state=='coding' else 1.,'last_event':tool.get(s) or (ev.get(s) or {}).get('type') or state,'last_ts':x.get('last_ts') or ''})
    return rows
def ops():
    a=activity();lookup,_=ws_lookup();tools={e.get('session_id'):e.get('tool') for e in legacy.read_events(500) if e.get('type')=='tool_start' and e.get('session_id')};sessions=[]
    for r in legacy.scan_transcripts(limit=16):
        s=r.get('session_id') or '';m=lookup.get(s,{});sessions.append({'id':s,'provider':'claude','project':m.get('project_title') or r.get('project_name') or 'Unknown','title':m.get('user_goal') or m.get('summary') or s[:8],'goal':m.get('user_goal') or '','summary':m.get('summary') or '','time':r.get('last_ts') or '','status':{'green':'running','yellow':'waiting','grey':'idle'}.get(r.get('status'),'idle'),'agent':'CLAUDE','tool':tools.get(s) or '','resume':s,'cwd':r.get('cwd') or m.get('cwd') or '','context_pct':0})
    for r in legacy.scan_codex_transcripts(limit=12):
        s=r.get('session_id') or '';sessions.append({'id':s,'provider':'codex','project':r.get('project_name') or 'Unknown','title':r.get('thread_name') or s[:8],'goal':r.get('thread_name') or '','summary':'','time':r.get('last_ts') or '','status':'idle','agent':'CODEX','tool':'','resume':s,'cwd':r.get('cwd') or '','context_pct':0})
    for r in legacy.scan_pi_transcripts(limit=8):
        s=r.get('session_id') or '';sessions.append({'id':s,'provider':'pi','project':r.get('project_name') or 'Unknown','title':r.get('thread_name') or s[:8],'goal':r.get('thread_name') or '','summary':'','time':r.get('last_ts') or '','status':'idle','agent':'PI','tool':'','resume':s,'cwd':r.get('cwd') or '','context_pct':0})
    for r in legacy.scan_openclaw_transcripts(limit=8):
        s=r.get('session_id') or '';sessions.append({'id':s,'provider':'openclaw','project':r.get('project_name') or 'Unknown','title':r.get('thread_name') or s[:8],'goal':r.get('thread_name') or '','summary':'','time':r.get('last_ts') or '','status':'idle','agent':'OPENCLAW','tool':'','resume':s,'cwd':r.get('cwd') or '','context_pct':0})
    for r in legacy.scan_hermes_transcripts(limit=8):
        s=r.get('session_id') or '';sessions.append({'id':s,'provider':'hermes','project':r.get('project_name') or 'Unknown','title':r.get('thread_name') or s[:8],'goal':r.get('thread_name') or '','summary':'','time':r.get('last_ts') or '','status':'idle','agent':'HERMES','tool':'','resume':s,'cwd':'','context_pct':0})
    sessions.sort(key=lambda x:x.get('time') or '',reverse=True);return{'kpi':{'active_hours_84d':a['active_hours']},'activity':a['days'],'quotas':quotas(),'schedule':schedule(),'sessions':sessions[:20],'agents':agents(),'flow':flow()}
def normalize(e):
    k=e.get('type') or 'event';typ,text,intensity=('tool_call',e.get('tool') or 'tool',.82) if k=='tool_start' else ('tool_result',e.get('tool') or 'tool',.58) if k=='tool_end' else ('completion',e.get('reason') or 'session stop',.66) if k=='stop' else (k,k,.5);s=e.get('session_id') or '';return{'type':typ,'source_type':k,'session_id':s,'agent':'CLAUDE-'+s[:8] if s else 'CLAUDE','project':e.get('project_name') or 'SYSTEM','text':text,'intensity':intensity,'ts':e.get('ts') or iso(datetime.now(timezone.utc))}
class Handler(legacy.Handler):
    def static(self,p,ctype):
        if not p.exists():return self.send_json({'error':'not found'},404)
        b=p.read_bytes();self.send_response(200);self.send_header('Content-Type',ctype);self.send_header('Cache-Control','no-cache');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    def do_GET(self):
        u=urlparse(self.path);p=u.path
        if p=='/':return self.static(SPATIAL_HTML,'text/html; charset=utf-8')
        if p=='/spatial_os.css':return self.static(SPATIAL_CSS,'text/css; charset=utf-8')
        if p=='/spatial_os.js':return self.static(SPATIAL_JS,'application/javascript; charset=utf-8')
        if p=='/legacy/dashboard':return self.static(legacy.DASHBOARD_FILE,'text/html; charset=utf-8')
        if p=='/api/world':return self.send_json(world())
        if p=='/api/ops':return self.send_json(ops())
        if p=='/api/activity':return self.send_json(activity())
        if p=='/api/activity/day':return self.send_json(day_payload((parse_qs(u.query).get('date') or [datetime.now(timezone.utc).date().isoformat()])[0]))
        if p=='/api/quotas':return self.send_json(quotas())
        if p=='/api/schedule':return self.send_json(schedule())
        if p=='/api/agents':return self.send_json(agents())
        if p=='/api/flow':return self.send_json(flow())
        if p=='/events/spatial':
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-cache');self.send_header('X-Accel-Buffering','no');self.send_header('Access-Control-Allow-Origin','*');self.end_headers();q=queue.Queue(maxsize=1024)
            with legacy._sse_lock:legacy._sse_clients.append(q)
            try:
                self.wfile.write(b': connected\n\n');self.wfile.flush();heart=time.time()
                while True:
                    try:
                        raw=q.get(timeout=5).decode('utf-8',errors='replace');line=next((x[6:] for x in raw.splitlines() if x.startswith('data: ')),'')
                        if line:self.wfile.write(('data: '+json.dumps(normalize(json.loads(line)),ensure_ascii=False)+'\n\n').encode());self.wfile.flush()
                    except queue.Empty:pass
                    except Exception:pass
                    if time.time()-heart>15:self.wfile.write(b': heartbeat\n\n');self.wfile.flush();heart=time.time()
            except:pass
            finally:
                with legacy._sse_lock:
                    if q in legacy._sse_clients:legacy._sse_clients.remove(q)
            return
        return super().do_GET()
if __name__=='__main__':
    legacy.DATA_DIR.mkdir(parents=True,exist_ok=True);legacy.SESSIONS_DIR.mkdir(parents=True,exist_ok=True);pid=legacy.DATA_DIR/'server.pid';pid.write_text(str(os.getpid()));threading.Thread(target=legacy.broadcast_thread,daemon=True).start();http.server.ThreadingHTTPServer.allow_reuse_address=False;srv=http.server.ThreadingHTTPServer(('127.0.0.1',PORT),Handler);print(f'Apocalypse Spatial OS running at http://localhost:{PORT}',flush=True)
    try:srv.serve_forever()
    finally:
        try:pid.unlink()
        except FileNotFoundError:pass
