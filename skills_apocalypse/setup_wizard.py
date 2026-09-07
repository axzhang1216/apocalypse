#!/usr/bin/env python3
"""Interactive first-run setup for Apocalypse.

Discovers existing agents/providers, asks which Plan usage rows to show, asks
which provider/model should power Apocalypse analysis, then writes private
Apocalypse-owned config without modifying any source agent configuration.
"""
from __future__ import annotations
import getpass, json, os, stat, sys
from pathlib import Path
from typing import Any
from agent_discovery import PLAN_NAMES, discover_internal

DATA_DIR=Path.home()/".claude"/"apocalypse";HARNESS_FILE=DATA_DIR/"harness.json";QUOTA_FILE=DATA_DIR/"quota_sources.json";SECRETS_FILE=DATA_DIR/"secrets.json";SETUP_FILE=DATA_DIR/"setup.json"

def _read_json(p):
 try:return json.loads(Path(p).read_text(encoding="utf-8"))
 except Exception:return None

def _write_json(p,v,private=False):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix(p.suffix+".tmp");tmp.write_text(json.dumps(v,ensure_ascii=False,indent=2)+"\n",encoding="utf-8");os.replace(tmp,p)
 if private:
  try:os.chmod(p,stat.S_IRUSR|stat.S_IWUSR)
  except Exception:pass

def _ask(prompt,default=""):
 suffix=f" [{default}]" if default else ""
 try:v=input(prompt+suffix+": ").strip()
 except EOFError:return default
 return v or default

def _yes(prompt,default=True):
 v=_ask(f"{prompt} ({'Y/n' if default else 'y/N'})","").lower();return default if not v else v in ("y","yes","1","true","是","好")

def _multi(prompt,items):
 print("\n"+prompt)
 for i,x in enumerate(items,1):print(f"  {i}. {x}")
 raw=_ask("输入编号，可逗号分隔；all=全部；none=不显示","all").lower()
 if raw in ("all","a","*"):return list(range(len(items)))
 if raw in ("none","n","0",""):return []
 out=[]
 for s in raw.replace("，",",").split(","):
  try:i=int(s.strip())-1
  except Exception:continue
  if 0<=i<len(items) and i not in out:out.append(i)
 return out

def _one(prompt,items,default=0):
 print("\n"+prompt)
 for i,x in enumerate(items,1):print(f"  {i}. {x}")
 while True:
  try:i=int(_ask("输入编号",str(default+1)))-1
  except Exception:i=-1
  if 0<=i<len(items):return i
  print("  无效编号，请重试。")

def _safe_auth(c):
 a=c.get("_auth")
 if not isinstance(a,dict):return None
 if a.get("kind")=="env":return {"kind":"env","name":a.get("name")}
 if a.get("kind")=="literal" and a.get("value"):
  s=_read_json(SECRETS_FILE);s=s if isinstance(s,dict) else {};s["analysis_api_key"]=str(a["value"]);_write_json(SECRETS_FILE,s,True);return {"kind":"file","path":str(SECRETS_FILE),"json_path":"analysis_api_key"}
 return None

def _display(c):
 ms=c.get("models") or [];mt=", ".join(ms[:3])+(" …" if len(ms)>3 else "");src=c.get("source_agent") or "config";plan=f" · {c['plan']}" if c.get("plan") else ""
 return f"{c.get('provider')} [{src}{plan}] · {c.get('transport')} · {c.get('availability')}"+(f" · {mt}" if mt else "")

def _rank(c):
 av={"verified":3,"configured":2,"unavailable":0}.get(c.get("availability"),1);direct=2 if c.get("transport") in ("anthropic_messages","openai_responses","openai_chat") else 1;return av,direct,len(c.get("models") or [])

def _analysis_config(c,model):
 out={"provider":c.get("provider"),"plan":c.get("plan"),"source_agent":c.get("source_agent"),"transport":c.get("transport"),"model":model}
 for k in ("base_url","executable","provider_override"):
  if c.get(k):out[k]=c[k]
 a=_safe_auth(c)
 if a:out["auth"]=a
 return out

def _normalize_url(v):
 s=str(v or "").strip().rstrip("/")
 if s and "://" not in s:s=("http://" if s.startswith("localhost") or s.startswith("127.") else "https://")+s
 return s

def _detect_gproxy(cs):
 old=_read_json(QUOTA_FILE)
 if isinstance(old,dict) and isinstance(old.get("gproxy"),dict) and old["gproxy"].get("base_url"):return _normalize_url(old["gproxy"]["base_url"])
 for c in cs:
  b=str(c.get("base_url") or "");t=(str(c.get("provider") or "")+" "+b).lower()
  if "gproxy" in t or "ao-xing-zhang.com" in t:return _normalize_url(b)
 return ""

def _quota_config(selected,cs,interactive=True):
 old=_read_json(QUOTA_FILE);cfg=old if isinstance(old,dict) else {};cfg["enabled_plans"]=selected
 if not selected:return cfg
 gp=cfg.get("gproxy") if isinstance(cfg.get("gproxy"),dict) else {};cfg["gproxy"]=gp
 if not interactive:return cfg
 default=_detect_gproxy(cs)
 print("\nPlan usage source\n  Apocalypse 当前优先从你的 GProxy quota-cycle API 读取 5H / WEEKLY。\n  模型 API key 与 GProxy admin 登录不是同一个凭据。")
 if default or _yes("配置 GProxy 作为 Plan usage source？",True):
  base=_normalize_url(_ask("GProxy base URL",default))
  if base:
   gp["base_url"]=base;gp["username"]=_ask("GProxy admin username",str(gp.get("username") or "admin"));existing=bool(gp.get("password_secret"));need=not existing or _yes("更新已保存的 GProxy admin password？",False)
   if need:
    try:pw=getpass.getpass("GProxy admin password: ").strip()
    except Exception:pw=""
    if pw:
     s=_read_json(SECRETS_FILE);s=s if isinstance(s,dict) else {};s["gproxy_admin_password"]=pw;_write_json(SECRETS_FILE,s,True);gp["password_secret"]="gproxy_admin_password";gp.pop("password",None)
 return cfg

def _verify_harness(interactive=True):
 import analysis_harness
 print("\nVerifying selected Apocalypse analysis model with one minimal request…")
 try:
  r=analysis_harness.test()
  if not r.get("ok"):raise RuntimeError("unexpected response: "+str(r.get("response")))
  print("  ✓ model invocation verified")
  return True,""
 except Exception as e:
  print(f"  × model invocation failed: {e}")
  if interactive and _yes("保留这个配置并继续？",False):return False,str(e)
  raise

def run(force=False,non_interactive=False):
 DATA_DIR.mkdir(parents=True,exist_ok=True)
 if SETUP_FILE.exists() and not force and non_interactive:return {"ok":True,"skipped":True,"reason":"already initialized"}
 print("\nAPOCALYPSE · FIRST-RUN SETUP\nScanning installed agents and their configured providers…\n");d=discover_internal(True);agents=d["agents"];cs=d["candidates"]
 if agents:
  print("Detected agents:")
  for a in agents:print(f"  ✓ {a['name']} · {a['version']} · {a['executable']}")
 else:print("Detected agents: none on PATH")
 print("\nDetected provider paths:")
 for c in sorted(cs,key=_rank,reverse=True):print(f"  {'✓' if c.get('availability')=='verified' else '~' if c.get('availability')=='configured' else '×'} {_display(c)}")
 usable=[]
 for name in PLAN_NAMES:
  p=d["plans"].get(name)
  if p and p.get("availability") in ("verified","configured"):usable.append(name)
 if non_interactive:selected=usable
 else:selected=[usable[i] for i in (_multi("你希望显示哪些 Plan 的 usage 在 Apocalypse 仪表盘？",[f"{p} · {d['plans'][p].get('availability')}" for p in usable]) if usable else [])]
 _write_json(QUOTA_FILE,_quota_config(selected,cs,not non_interactive),True)
 eligible=[c for c in cs if c.get("availability") in ("verified","configured") and c.get("transport") in ("anthropic_messages","openai_responses","openai_chat","claude_cli","codex_cli","hermes_cli")];eligible.sort(key=_rank,reverse=True)
 if not eligible:raise RuntimeError("No usable analysis provider was detected. Configure/login to an agent or model provider, then run apocalypse-ui init again.")
 chosen=eligible[0] if non_interactive else eligible[_one("你希望哪个 provider 作为 Apocalypse 的分析模型来源？",[_display(c) for c in eligible])]
 models=[m for m in chosen.get("models") or [] if m]
 if not models:
  model=_ask("该 provider 未列出模型，请输入 analysis model ID") if not non_interactive else ""
  if not model:raise RuntimeError("Analysis model ID is required")
 elif len(models)==1:model=models[0]
 elif non_interactive:model=models[0]
 else:model=models[_one(f"你希望 {chosen.get('provider')} 的哪个模型作为 Apocalypse 的分析模型？",models)]
 harness={"version":1,"analysis_model":_analysis_config(chosen,model),"jobs":{"workspace_session_analysis":True,"discussion_decision_analysis":True,"compact_conversation_analysis":True,"schedule_analysis":True,"agent_worklog_analysis":True}}
 _write_json(HARNESS_FILE,harness,True);verified,error=_verify_harness(not non_interactive)
 setup={"version":1,"initialized":True,"agents":[{"id":a["id"],"name":a["name"],"version":a["version"]} for a in agents],"quota_plans":selected,"analysis_provider":chosen.get("provider"),"analysis_model":model,"analysis_transport":chosen.get("transport"),"analysis_verified":verified,"analysis_error":error}
 _write_json(SETUP_FILE,setup,True)
 print("\nConfigured:");print("  LLM QUOTA      → "+(", ".join(selected) if selected else "none"));print(f"  ANALYSIS MODEL → {chosen.get('provider')} / {model} [{chosen.get('transport')}]");print(f"  Harness        → {HARNESS_FILE}")
 return {"ok":True,**setup}
if __name__=="__main__":
 import argparse
 p=argparse.ArgumentParser(description="Configure Apocalypse from installed agents");p.add_argument("--force",action="store_true");p.add_argument("--non-interactive",action="store_true");a=p.parse_args()
 try:
  r=run(a.force,a.non_interactive)
  if a.non_interactive:print(json.dumps(r,ensure_ascii=False,indent=2))
 except Exception as e:print(f"Setup failed: {e}",file=sys.stderr);raise SystemExit(1)
