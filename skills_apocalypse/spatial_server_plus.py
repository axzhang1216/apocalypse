#!/usr/bin/env python3
"""Apocalypse Spatial OS runtime extensions."""
from __future__ import annotations

import contextlib, io, json, os, shutil, threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import app_lifecycle
import onboarding
import ops_analysis
import quota_adapters
import spatial_server as spatial
import workspace_init

PORT=spatial.PORT;http=spatial.http;core=spatial.legacy
ANALYSIS_FILE=core.DATA_DIR/"ops_analysis.json"
ONBOARDING_JS=Path(__file__).resolve().parent/"onboarding_ui.js"
spatial.quotas=quota_adapters.get_quotas


def _pid_alive(pid):
 try:
  pid=int(pid)
  if pid<=0:return False
  os.kill(pid,0);return True
 except Exception:return False


def _live_claude_process(session_id):
 d=Path.home()/".claude"/"sessions"
 if not d.exists():return None
 for p in d.glob("*.json"):
  try:x=json.loads(p.read_text(encoding="utf-8",errors="replace"))
  except Exception:continue
  if str(x.get("sessionId") or x.get("session_id") or "")!=session_id:continue
  pid=x.get("pid")
  if pid is None:
   try:pid=int(p.stem)
   except Exception:pid=None
  if pid and _pid_alive(pid):return {"pid":int(pid),"status":x.get("status") or "running"}
 return None


def _maintenance_target(session_id):
 if not session_id or "/" in session_id or "\\" in session_id or session_id.startswith("."):return None,"bad id",400
 p=core._find_transcript_path(session_id)
 if not p:return None,"session transcript not found",404
 live=_live_claude_process(session_id)
 if live:return None,f"session is still open in Claude Code (pid {live['pid']}, status {live['status']}); close/stop it before maintenance",409
 return p,None,200


def _atomic_backup_replace(path,before,rows,session_id,kind):
 try:
  now=path.stat()
  if now.st_size!=before.st_size or now.st_mtime_ns!=before.st_mtime_ns:return None,"transcript changed while being inspected; maintenance aborted",409
 except Exception as e:return None,f"could not re-check transcript: {e}",500
 backup_dir=core.DATA_DIR/"repair_backups";stamp=datetime.now().strftime("%Y%m%d-%H%M%S-%f");backup=backup_dir/f"{session_id}-{kind}-{stamp}.jsonl";tmp=path.with_name(f".{path.name}.{kind}-{os.getpid()}.tmp")
 try:
  backup_dir.mkdir(parents=True,exist_ok=True);shutil.copy2(path,backup)
  with open(tmp,"wb") as f:
   for row in rows:f.write(row+b"\n")
   f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 except Exception as e:
  try:tmp.unlink(missing_ok=True)
  except Exception:pass
  return None,f"maintenance write failed: {e}",500
 return backup,None,200


def _invalidate_compact_cache(session_id):
 try:
  ws=core._load_workspace()
  if not isinstance(ws,dict):return
  cache=ws.get("_compact_cache")
  key=f"compact_{session_id}"
  if not isinstance(cache,dict) or key not in cache:return
  del cache[key];tmp=core.WORKSPACE_FILE.with_suffix(".json.tmp");tmp.write_text(json.dumps(ws,ensure_ascii=False,indent=2),encoding="utf-8");os.replace(tmp,core.WORKSPACE_FILE)
 except Exception:pass


def repair_conversation(session_id):
 path,error,status=_maintenance_target(session_id)
 if error:return None,error,status
 try:before=path.stat();raws=path.read_bytes().splitlines()
 except Exception as e:return None,f"could not read transcript: {e}",500
 good=[];bad=[];total=0
 for i,raw in enumerate(raws,1):
  if not raw.strip():continue
  total+=1
  try:json.loads(raw.decode("utf-8"));good.append(raw)
  except Exception as e:bad.append({"line":i,"error":str(e)[:160]})
 if not bad:return {"ok":True,"changed":False,"session_id":session_id,"total_records":total,"valid_records":len(good),"removed_records":0},None,200
 backup,error,status=_atomic_backup_replace(path,before,good,session_id,"repair")
 if error:return None,error,status
 _invalidate_compact_cache(session_id)
 return {"ok":True,"changed":True,"session_id":session_id,"total_records":total,"valid_records":len(good),"removed_records":len(bad),"removed":bad[:50],"backup":str(backup),"transcript":str(path)},None,200


def clean_non_text_context(session_id):
 path,error,status=_maintenance_target(session_id)
 if error:return None,error,status
 try:before=path.stat();raws=path.read_bytes().splitlines()
 except Exception as e:return None,f"could not read transcript: {e}",500
 out=[];types=Counter();malformed=[];touched=placeholders=total=0
 for i,raw in enumerate(raws,1):
  if not raw.strip():continue
  total+=1
  try:r=json.loads(raw.decode("utf-8"))
  except Exception as e:malformed.append({"line":i,"error":str(e)[:160]});continue
  changed=False;msg=r.get("message")
  if r.get("type") in ("user","assistant") and isinstance(msg,dict):
   content=msg.get("content")
   if isinstance(content,list):
    text=[]
    for block in content:
     if isinstance(block,dict) and block.get("type")=="text":text.append(block)
     elif isinstance(block,str):
      if block:text.append({"type":"text","text":block})
      changed=True;types["raw_string_block"]+=1
     else:
      changed=True;types[str(block.get("type") if isinstance(block,dict) else type(block).__name__)]+=1
    if changed:
     touched+=1
     if not text:text=[{"type":"text","text":"[non-text context removed by Apocalypse]"}];placeholders+=1
     msg["content"]=text
   elif isinstance(content,dict) and content.get("type")!="text":
    types[str(content.get("type") or "unknown")]+=1;msg["content"]=[{"type":"text","text":"[non-text context removed by Apocalypse]"}];changed=True;touched+=1;placeholders+=1
   elif content is not None and not isinstance(content,str):
    types[type(content).__name__]+=1;msg["content"]="[non-text context removed by Apocalypse]";changed=True;touched+=1;placeholders+=1
  out.append(json.dumps(r,ensure_ascii=False,separators=(",",":")).encode("utf-8") if changed else raw)
 removed=sum(types.values())
 if not removed and not malformed:return {"ok":True,"changed":False,"session_id":session_id,"total_records":total,"removed_blocks":0,"removed_types":{}},None,200
 backup,error,status=_atomic_backup_replace(path,before,out,session_id,"text-only")
 if error:return None,error,status
 _invalidate_compact_cache(session_id)
 return {"ok":True,"changed":True,"session_id":session_id,"total_records":total,"removed_blocks":removed,"removed_types":dict(sorted(types.items())),"messages_cleaned":touched,"placeholder_messages":placeholders,"repaired_records":len(malformed),"malformed":malformed[:50],"backup":str(backup),"transcript":str(path)},None,200


def _cached_analysis():
 try:return json.loads(ANALYSIS_FILE.read_text(encoding="utf-8"))
 except Exception:return {"generated_at":None,"schedule":None,"worklog":None}


def _workspace_update_in_process():
 buf=io.StringIO()
 with contextlib.redirect_stdout(buf):workspace_init.run(incremental=True)
 events=[]
 for line in buf.getvalue().splitlines():
  try:events.append(json.loads(line))
  except Exception:continue
 project_events=[e for e in events if e.get("type")=="project_done"];done=next((e for e in reversed(events) if e.get("type")=="done"),{});ws=core._load_workspace() or {};details=[]
 for evt in project_events:
  name=evt.get("project") or "";count=int(evt.get("sessions") or 0);record=next((p for p in (ws.get("projects") or {}).values() if p.get("name")==name),None);recent=[]
  if record:
   analyzed=record.get("analyzed_sessions") or {}
   for sid in sorted(analyzed,key=lambda s:analyzed[s].get("ts") or "",reverse=True)[:count]:
    s=analyzed[sid];recent.append({"goal":s.get("user_goal",""),"summary":s.get("summary",""),"category":s.get("category","other")})
  details.append({"name":name,"title":record.get("title","") if record else "","new_sessions":count,"sessions":recent})
 return {"ok":True,"total_new":int(done.get("total_sessions") or 0),"projects_updated":len(project_events),"projects":details}


def _read_json_body(handler,max_bytes=131072):
 try:length=int(handler.headers.get("Content-Length") or 0)
 except Exception:length=0
 if length<0 or length>max_bytes:raise ValueError("request body too large")
 if not length:return {}
 try:return json.loads(handler.rfile.read(length).decode("utf-8"))
 except Exception as e:raise ValueError("invalid JSON body") from e


class Handler(spatial.Handler):
 def do_GET(self):
  path=urlparse(self.path).path
  if path=="/":
   try:
    html=spatial.SPATIAL_HTML.read_text(encoding="utf-8");html=html.replace("</body>",'<script src="/onboarding_ui.js"></script></body>');body=html.encode("utf-8")
    self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Cache-Control","no-cache");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body);return
   except Exception as e:return self.send_json({"error":str(e)},500)
  if path=="/onboarding_ui.js":return self.static(ONBOARDING_JS,"application/javascript; charset=utf-8")
  if path=="/api/onboarding/status":return self.send_json(onboarding.status())
  if path=="/api/onboarding/discover":
   try:return self.send_json(onboarding.discover())
   except Exception as e:return self.send_json({"ok":False,"error":str(e)},500)
  if path=="/api/quotas":return self.send_json(quota_adapters.get_quotas())
  if path=="/api/analysis":return self.send_json(_cached_analysis())
  if path=="/api/settings/status":return self.send_json({**app_lifecycle.app_status(),**onboarding.status()})
  if path=="/api/settings/update":
   try:return self.send_json(app_lifecycle.check_update())
   except Exception as e:return self.send_json({"ok":False,"error":str(e)},502)
  return super().do_GET()

 def do_POST(self):
  path=urlparse(self.path).path
  if path=="/api/onboarding/complete":
   try:return self.send_json(onboarding.complete(_read_json_body(self)))
   except ValueError as e:return self.send_json({"ok":False,"error":str(e)},400)
   except Exception as e:return self.send_json({"ok":False,"error":str(e)},500)
  if path=="/api/settings/update":
   try:return self.send_json(app_lifecycle.apply_update())
   except Exception as e:return self.send_json({"ok":False,"error":str(e)},500)
  if path=="/api/workspace/update":
   try:return self.send_json(_workspace_update_in_process())
   except Exception as e:return self.send_json({"ok":False,"error":str(e)},500)
  if path=="/api/analysis/refresh":
   try:return self.send_json({"ok":True,**ops_analysis.refresh(schedule=True,worklog=True)})
   except Exception as e:return self.send_json({"ok":False,"error":str(e)},500)
  prefix="/api/sessions2/"
  if path.startswith(prefix) and path.endswith("/clean-context"):
   sid=path[len(prefix):-len("/clean-context")];payload,error,status=clean_non_text_context(sid);return self.send_json({"ok":False,"error":error},status) if error else self.send_json(payload,status)
  if path.startswith(prefix) and path.endswith("/repair"):
   sid=path[len(prefix):-len("/repair")];payload,error,status=repair_conversation(sid);return self.send_json({"ok":False,"error":error},status) if error else self.send_json(payload,status)
  return super().do_POST()


if __name__=="__main__":
 core.DATA_DIR.mkdir(parents=True,exist_ok=True);core.SESSIONS_DIR.mkdir(parents=True,exist_ok=True);pid=core.DATA_DIR/"server.pid";pid.write_text(str(os.getpid()),encoding="utf-8");threading.Thread(target=core.broadcast_thread,daemon=True).start();http.server.ThreadingHTTPServer.allow_reuse_address=False;srv=http.server.ThreadingHTTPServer(("127.0.0.1",PORT),Handler);print(f"Apocalypse Spatial OS running at http://localhost:{PORT}",flush=True)
 try:srv.serve_forever()
 finally:
  try:pid.unlink()
  except FileNotFoundError:pass
