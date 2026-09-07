#!/usr/bin/env python3
"""Read-only discovery of installed agents, providers and model choices."""
from __future__ import annotations
import json, os, re, shutil, subprocess, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HOME=Path.home()
AGENTS={"claude":{"commands":("claude",),"label":"Claude Code"},"codex":{"commands":("codex",),"label":"Codex"},"pi":{"commands":("pi",),"label":"Pi"},"hermes":{"commands":("hermes","hermes-cli"),"label":"Hermes"},"openclaw":{"commands":("openclaw",),"label":"OpenClaw"}}
PLAN_NAMES=("Claude","OpenAI","Grok","Volc Agent","MiniMax Intl")

def _json(p):
 try:return json.loads(Path(p).read_text(encoding="utf-8"))
 except Exception:return None

def _toml(p):
 try:
  import tomllib;return tomllib.loads(Path(p).read_text(encoding="utf-8"))
 except Exception:return None

def _version(exe):
 for a in (("--version",),("version",)):
  try:
   kw={"capture_output":True,"text":True,"timeout":3}
   if os.name=="nt":kw["creationflags"]=getattr(subprocess,"CREATE_NO_WINDOW",0)
   p=subprocess.run([exe,*a],**kw);lines=(p.stdout or p.stderr or "").strip().splitlines()
   if lines:return lines[0][:160]
  except Exception:pass
 return "installed"

def installed_agents():
 out=[]
 for aid,m in AGENTS.items():
  exe=next((shutil.which(c) for c in m["commands"] if shutil.which(c)),None)
  if exe:out.append({"id":aid,"name":m["label"],"executable":exe,"version":_version(exe)})
 return out

def infer_plan(*parts):
 t=" ".join(str(x or "") for x in parts).lower()
 if any(x in t for x in ("anthropic","claude")):return "Claude"
 if any(x in t for x in ("grok","x.ai","xai","x-ai")):return "Grok"
 if "minimax" in t:return "MiniMax Intl"
 if any(x in t for x in ("volc","volcengine","doubao","ark.cn","seed-","火山","豆包")):return "Volc Agent"
 if any(x in t for x in ("openai","chatgpt","codex","gpt-","o1","o3","o4")):return "OpenAI"
 return None

def _transport(api,plan=None):
 s=str(api or "").lower().replace("-","_")
 if "anthropic" in s or "claude_messages" in s or s=="messages":return "anthropic_messages"
 if "response" in s:return "openai_responses"
 if "chat" in s or "openai" in s:return "openai_chat"
 return "anthropic_messages" if plan=="Claude" else "openai_responses"

def _clean_base(v):
 s=str(v or "").strip().rstrip("/")
 if s and "://" not in s:
  s=("http://" if s.startswith("localhost") or re.match(r"^\d+\.\d+\.\d+\.\d+",s) else "https://")+s
 return s

def _candidate(source_agent,provider,plan,transport,models=None,base_url="",auth=None,executable="",configured=True,detail="",provider_override=""):
 return {"source_agent":source_agent,"provider":provider,"plan":plan,"transport":transport,"models":list(dict.fromkeys(str(x) for x in (models or []) if x)),"base_url":_clean_base(base_url),"executable":executable,"provider_override":provider_override,"availability":"configured" if configured else "unavailable","detail":detail,"_auth":auth}

def _auth_value(a):
 if not isinstance(a,dict):return None
 if a.get("kind")=="env":return os.environ.get(str(a.get("name") or ""))
 if a.get("kind")=="literal":return str(a.get("value") or "") or None
 return None

def _models_url(base):
 b=_clean_base(base)
 if not b:return ""
 path=urllib.parse.urlsplit(b).path.rstrip("/")
 return b+"/models" if path.endswith(("/v1","/v1beta")) else b+"/v1/models" if not path else b+"/models"

def _probe_http(c):
 token=_auth_value(c.get("_auth"));h={"accept":"application/json","user-agent":"Apocalypse-Setup/1"}
 if token:h.update({"authorization":"Bearer "+token,"x-api-key":token})
 try:
  with urllib.request.urlopen(urllib.request.Request(_models_url(c["base_url"]),headers=h),timeout=4) as r:raw=r.read(1500000);status=r.status
  data=json.loads(raw.decode("utf-8",errors="replace")) if raw else {};items=data.get("data") if isinstance(data,dict) else None
  if not isinstance(items,list) and isinstance(data,dict):items=data.get("models")
  models=[]
  for x in items or []:
   mid=(x.get("id") or x.get("name")) if isinstance(x,dict) else x
   if mid and str(mid) not in models:models.append(str(mid))
  return "verified",models[:100],f"HTTP {status}"
 except urllib.error.HTTPError as e:return ("unavailable" if e.code in (401,403) else "configured"),[],f"models probe HTTP {e.code}"
 except Exception as e:return "configured",[],f"models probe unavailable: {type(e).__name__}"

def _claude(a):
 settings=_json(HOME/".claude/settings.json") or {};local=_json(HOME/".claude/settings.local.json") or {};env={}
 for c in (settings,local):
  if isinstance(c,dict) and isinstance(c.get("env"),dict):env.update(c["env"])
 models=[]
 for k in ("ANTHROPIC_MODEL","ANTHROPIC_DEFAULT_OPUS_MODEL","ANTHROPIC_DEFAULT_SONNET_MODEL","ANTHROPIC_DEFAULT_HAIKU_MODEL"):
  if env.get(k):models.append(str(env[k]))
 models += [x for x in ("sonnet","opus","haiku") if x not in models]
 auth=None
 for k in ("ANTHROPIC_AUTH_TOKEN","ANTHROPIC_API_KEY"):
  if env.get(k):auth={"kind":"literal","value":env[k],"source":"Claude settings"};break
 if not auth and os.environ.get("ANTHROPIC_API_KEY"):auth={"kind":"env","name":"ANTHROPIC_API_KEY"}
 out=[]
 if a:out.append(_candidate("claude","Anthropic / Claude Code","Claude","claude_cli",models=models,executable=a["executable"],configured=bool((HOME/".claude/.credentials.json").exists() or auth or settings),detail="native Claude Code login/config"))
 if env.get("ANTHROPIC_BASE_URL"):out.append(_candidate("claude","Claude configured API",infer_plan(env.get("ANTHROPIC_BASE_URL"),*models),"anthropic_messages",models,env["ANTHROPIC_BASE_URL"],auth,configured=bool(auth),detail="from Claude settings"))
 return out

def _codex(a):
 cfg=_toml(HOME/".codex/config.toml") or {};model=str(cfg.get("model") or "") if isinstance(cfg,dict) else "";out=[]
 if a:out.append(_candidate("codex","OpenAI / Codex","OpenAI","codex_cli",[model] if model else ["(Codex default)"],executable=a["executable"],configured=bool((HOME/".codex/auth.json").exists() or cfg or os.environ.get("OPENAI_API_KEY")),detail="native Codex login/config"))
 ps=cfg.get("model_providers") if isinstance(cfg,dict) else None
 if isinstance(ps,dict):
  for name,p in ps.items():
   if not isinstance(p,dict):continue
   base=p.get("base_url") or p.get("baseUrl") or "";ek=p.get("env_key") or p.get("envKey");auth={"kind":"env","name":str(ek)} if ek else None;plan=infer_plan(name,model,base)
   out.append(_candidate("codex",str(name),plan,_transport(p.get("wire_api") or p.get("api"),plan),[model] if model else [],base,auth,configured=not ek or bool(os.environ.get(str(ek))),detail="from ~/.codex/config.toml"))
 return out

def _dotenv(path):
 out={}
 try:lines=Path(path).read_text(encoding="utf-8",errors="replace").splitlines()
 except Exception:return out
 for line in lines:
  line=line.strip()
  if not line or line.startswith("#") or "=" not in line:continue
  k,v=line.split("=",1);out[k.strip()]=v.strip().strip('"').strip("'")
 return out

def _hermes_yaml():
 p=HOME/".hermes/config.yaml";result={}
 try:lines=p.read_text(encoding="utf-8",errors="replace").splitlines()
 except Exception:return result
 in_model=False;indent=None
 for raw in lines:
  if not raw.strip() or raw.lstrip().startswith("#"):continue
  lead=len(raw)-len(raw.lstrip())
  if lead==0:in_model=raw.strip()=="model:";indent=None;continue
  if not in_model:continue
  if indent is None:indent=lead
  if lead<indent:break
  line=raw.strip()
  if ":" not in line:continue
  k,v=line.split(":",1);v=v.strip().strip('"').strip("'")
  if k.strip() in ("provider","default","model","base_url") and v:result[k.strip()]=v
 return result

def _hermes(a):
 if not a:return []
 cfg=_hermes_yaml();env=_dotenv(HOME/".hermes/.env");auth_exists=(HOME/".hermes/auth.json").exists();provider=cfg.get("provider") or "auto";model=cfg.get("model") or cfg.get("default") or "";base=cfg.get("base_url") or "";plan=infer_plan(provider,model,base);configured=bool(cfg or env or auth_exists)
 return [_candidate("hermes",f"Hermes / {provider}",plan,"hermes_cli",[model] if model else ["(Hermes default)"],base_url=base,executable=a["executable"],configured=configured,detail="~/.hermes/config.yaml + native auth",provider_override=provider)]

def _secret(block):
 for k in ("apiKey","api_key","token","authToken","auth_token"):
  v=block.get(k) if isinstance(block,dict) else None
  if isinstance(v,str) and v.strip():
   m=re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",v.strip());return {"kind":"env","name":m.group(1)} if m else {"kind":"literal","value":v.strip(),"source":"agent config"}
 return None

def _model_ids(v):
 if isinstance(v,str):return [v]
 out=[]
 for x in v if isinstance(v,list) else []:
  mid=x if isinstance(x,str) else (x.get("id") or x.get("model") or x.get("name")) if isinstance(x,dict) else None
  if mid and str(mid) not in out:out.append(str(mid))
 return out

def _walk(node,path="root"):
 if isinstance(node,dict):
  base=node.get("baseUrl") or node.get("base_url") or node.get("apiBase") or node.get("api_base");models=_model_ids(node.get("models") or node.get("model_ids") or [])
  if node.get("model") and not isinstance(node.get("model"),(dict,list)):models.append(str(node["model"]))
  api=node.get("api") or node.get("protocol") or node.get("wire_api") or node.get("type")
  if base or (models and any(k in node for k in ("apiKey","api_key","token","env_key","envKey"))):yield path,node,str(base or ""),models,str(api or "")
  for k,v in node.items():
   if isinstance(v,(dict,list)):yield from _walk(v,path+"."+str(k))
 elif isinstance(node,list):
  for i,v in enumerate(node):
   if isinstance(v,(dict,list)):yield from _walk(v,path+f"[{i}]")

def _generic(aid):
 paths={"openclaw":[HOME/".openclaw/openclaw.json",HOME/".openclaw/config.json",HOME/".openclaw/settings.json"],"pi":[HOME/".pi/agent/settings.json",HOME/".pi/settings.json",HOME/".config/pi/settings.json"]}.get(aid,[]);out=[];seen=set()
 for p in paths:
  data=_json(p)
  if data is None:continue
  for jp,b,base,models,api in _walk(data):
   plan=infer_plan(jp,base,api,*models);key=(base,tuple(models),api,plan)
   if key in seen:continue
   seen.add(key);auth=_secret(b);out.append(_candidate(aid,jp.split(".")[-1],plan,_transport(api,plan),models,base,auth,configured=bool(base and auth),detail=f"from {p.name}:{jp}"))
 return out

def discover_internal(probe=True):
 agents=installed_agents();amap={a["id"]:a for a in agents};c=_claude(amap.get("claude"))+_codex(amap.get("codex"))+_hermes(amap.get("hermes"))+_generic("openclaw")+_generic("pi");ded=[];seen=set()
 for x in c:
  k=(x["source_agent"],x["transport"],x["base_url"],tuple(x["models"]),x["provider"])
  if k not in seen:seen.add(k);ded.append(x)
 c=ded
 if probe:
  for x in c:
   if x["transport"] in ("claude_cli","codex_cli","hermes_cli"):continue
   if x.get("base_url") and x["availability"]!="unavailable":
    st,models,detail=_probe_http(x);x["availability"]=st;x["detail"]=(x.get("detail","")+"; "+detail).strip("; ");x["models"]=list(dict.fromkeys(x["models"]+models))
 plans={};rank={"verified":3,"configured":2,"unavailable":0}
 for name in PLAN_NAMES:
  rows=[x for x in c if x.get("plan")==name]
  if rows:
   best=max(rows,key=lambda x:rank.get(x.get("availability"),1));plans[name]={"availability":best["availability"],"sources":sorted(set(x["source_agent"] for x in rows)),"models":list(dict.fromkeys(m for x in rows for m in x["models"]))[:100]}
 return {"agents":agents,"candidates":c,"plans":plans}

def discover(probe=True):
 d=discover_internal(probe);return {"agents":d["agents"],"providers":[{k:v for k,v in x.items() if not k.startswith("_")} for x in d["candidates"]],"plans":d["plans"]}
if __name__=="__main__":print(json.dumps(discover(True),ensure_ascii=False,indent=2))
