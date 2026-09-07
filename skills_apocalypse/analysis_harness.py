#!/usr/bin/env python3
"""Provider-independent LLM harness owned by Apocalypse."""
from __future__ import annotations
import json, os, re, subprocess, urllib.error, urllib.request
from pathlib import Path
from typing import Any

DATA_DIR=Path.home()/".claude"/"apocalypse";CONFIG_FILE=DATA_DIR/"harness.json"
class HarnessError(RuntimeError):pass

def load_config():
 try:cfg=json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
 except FileNotFoundError as e:raise HarnessError("Apocalypse analysis model is not configured. Run: apocalypse-ui init") from e
 except Exception as e:raise HarnessError(f"Could not read {CONFIG_FILE}: {e}") from e
 if not isinstance(cfg,dict) or not isinstance(cfg.get("analysis_model"),dict):raise HarnessError("Invalid harness.json: missing analysis_model")
 return cfg

def model_config():return dict(load_config()["analysis_model"])

def _secret(a):
 if not isinstance(a,dict):return None
 if a.get("kind")=="env":return os.environ.get(str(a.get("name") or ""))
 if a.get("kind")=="file":
  try:
   p=Path(os.path.expandvars(os.path.expanduser(str(a.get("path") or ""))));cur=json.loads(p.read_text(encoding="utf-8"))
   for part in str(a.get("json_path") or "").split("."):
    if part:cur=cur[part]
   return str(cur) if cur is not None else None
  except Exception:return None
 if a.get("kind")=="literal":return str(a.get("value") or "") or None
 return None

def _headers(c,anthropic=False):
 h={"content-type":"application/json","accept":"application/json","user-agent":"Apocalypse-Harness/1"};token=_secret(c.get("auth"))
 if token:
  if anthropic:h.update({"x-api-key":token,"anthropic-version":"2023-06-01"})
  else:h["authorization"]="Bearer "+token
 for k,v in (c.get("headers") or {}).items():
  if isinstance(k,str) and isinstance(v,str):h[k]=os.path.expandvars(v)
 return h

def _url(base,suffix):
 b=str(base or "").strip().rstrip("/")
 if not b:raise HarnessError("Selected HTTP analysis provider has no base_url")
 if "://" not in b:b=("http://" if b.startswith("localhost") or re.match(r"^\d+\.\d+\.\d+\.\d+",b) else "https://")+b
 if b.endswith(suffix):return b
 if b.endswith("/v1") and suffix.startswith("/v1/"):return b+suffix[len("/v1"):]
 return b+suffix

def _post(url,payload,headers,timeout):
 req=urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode(),headers=headers,method="POST")
 try:
  with urllib.request.urlopen(req,timeout=timeout) as r:raw=r.read()
 except urllib.error.HTTPError as e:
  try:detail=e.read().decode(errors="replace")[:1000]
  except Exception:detail=""
  raise HarnessError(f"Provider HTTP {e.code}: {detail or e.reason}") from e
 except Exception as e:raise HarnessError(f"Provider request failed: {e}") from e
 try:return json.loads(raw.decode("utf-8",errors="replace"))
 except Exception as e:raise HarnessError("Provider returned non-JSON output") from e

def _anthropic(c,prompt,max_tokens,system,timeout):
 payload={"model":c["model"],"max_tokens":max_tokens,"messages":[{"role":"user","content":prompt}]}
 if system:payload["system"]=system
 d=_post(_url(c.get("base_url") or "https://api.anthropic.com","/v1/messages"),payload,_headers(c,True),timeout);content=d.get("content") if isinstance(d,dict) else None
 if isinstance(content,list):
  text="".join(str(x.get("text") or "") for x in content if isinstance(x,dict) and x.get("type") in (None,"text"))
  if text.strip():return text.strip()
 raise HarnessError("Anthropic-compatible response did not contain text")

def _responses_text(d):
 if isinstance(d.get("output_text"),str) and d["output_text"].strip():return d["output_text"].strip()
 out=[]
 for item in d.get("output") or []:
  if isinstance(item,dict):
   for x in item.get("content") or []:
    if isinstance(x,dict) and x.get("type") in ("output_text","text") and x.get("text"):out.append(str(x["text"]))
 return "\n".join(out).strip()

def _responses(c,prompt,max_tokens,system,timeout):
 text=prompt if not system else system+"\n\n"+prompt;d=_post(_url(c.get("base_url") or "https://api.openai.com","/v1/responses"),{"model":c["model"],"input":text,"max_output_tokens":max_tokens},_headers(c),timeout);out=_responses_text(d)
 if out:return out
 raise HarnessError("Responses-compatible response did not contain output_text")

def _chat(c,prompt,max_tokens,system,timeout):
 ms=[]
 if system:ms.append({"role":"system","content":system})
 ms.append({"role":"user","content":prompt});d=_post(_url(c.get("base_url") or "https://api.openai.com","/v1/chat/completions"),{"model":c["model"],"messages":ms,"max_tokens":max_tokens},_headers(c),timeout)
 try:
  v=d["choices"][0]["message"]["content"]
  if isinstance(v,str) and v.strip():return v.strip()
 except Exception:pass
 raise HarnessError("Chat-compatible response did not contain assistant text")

def _run(args,prompt,timeout):
 kw={"input":prompt,"capture_output":True,"text":True,"timeout":timeout,"env":os.environ.copy()}
 if os.name=="nt":kw["creationflags"]=getattr(subprocess,"CREATE_NO_WINDOW",0)
 try:p=subprocess.run(args,**kw)
 except subprocess.TimeoutExpired as e:raise HarnessError(f"Analysis CLI timed out after {timeout:.0f}s") from e
 except Exception as e:raise HarnessError(f"Could not run analysis CLI: {e}") from e
 if p.returncode!=0:raise HarnessError((p.stderr or p.stdout or f"CLI exited {p.returncode}").strip()[:1200])
 if not (p.stdout or "").strip():raise HarnessError("Analysis CLI returned empty output")
 return p.stdout.strip()

def _claude(c,prompt,system,timeout):
 full=prompt if not system else system+"\n\n"+prompt;return _run([str(c.get("executable") or "claude"),"-p","--model",str(c.get("model") or "sonnet"),"--output-format","text"],full,timeout)

def _codex(c,prompt,system,timeout):
 full=prompt if not system else system+"\n\n"+prompt;args=[str(c.get("executable") or "codex"),"exec","--skip-git-repo-check"]
 if c.get("model") and c["model"]!="(Codex default)":args += ["--model",str(c["model"])]
 return _run(args+["-"],full,timeout)

def _hermes(c,prompt,system,timeout):
 """Hermes documents `-z` as pure one-shot: final answer only."""
 full=prompt if not system else system+"\n\n"+prompt;args=[str(c.get("executable") or "hermes"),"-z"]
 provider=c.get("provider_override");model=c.get("model")
 if provider and provider!="auto":args += ["--provider",str(provider)]
 if model and model!="(Hermes default)":args += ["--model",str(model)]
 # `-z` takes the prompt argument. Use stdin only for the model-independent wrappers.
 args += [full]
 return _run(args,"",timeout)

def complete(prompt,*,max_tokens=1024,system=None,timeout=120.0,model_override=None):
 c=model_config()
 if model_override:c["model"]=model_override
 t=str(c.get("transport") or "")
 if t=="anthropic_messages":return _anthropic(c,prompt,max_tokens,system,timeout)
 if t=="openai_responses":return _responses(c,prompt,max_tokens,system,timeout)
 if t=="openai_chat":return _chat(c,prompt,max_tokens,system,timeout)
 if t=="claude_cli":return _claude(c,prompt,system,timeout)
 if t=="codex_cli":return _codex(c,prompt,system,timeout)
 if t=="hermes_cli":return _hermes(c,prompt,system,timeout)
 raise HarnessError(f"Unsupported Apocalypse analysis transport: {t}")

def complete_json(prompt,*,max_tokens=1024,system=None,timeout=120.0):
 text=complete(prompt,max_tokens=max_tokens,system=system,timeout=timeout).strip()
 if text.startswith("```"):
  ls=text.splitlines();text="\n".join(ls[1:-1]).strip() if len(ls)>2 else text
 try:return json.loads(text)
 except Exception:
  m=re.search(r"(?:\{|\[).*(?:\}|\])",text,re.S)
  if m:
   try:return json.loads(m.group(0))
   except Exception:pass
  raise HarnessError("Analysis model returned invalid JSON")

def test():
 c=model_config();text=complete("Reply with exactly APOCALYPSE_OK and nothing else.",max_tokens=32,timeout=60);return {"ok":"APOCALYPSE_OK" in text,"provider":c.get("provider"),"model":c.get("model"),"transport":c.get("transport"),"response":text[:120]}
if __name__=="__main__":
 import argparse
 p=argparse.ArgumentParser();p.add_argument("--test",action="store_true");p.add_argument("prompt",nargs="?");a=p.parse_args()
 try:
  r=test() if a.test else complete(a.prompt or "Reply with APOCALYPSE_OK");print(json.dumps(r,ensure_ascii=False,indent=2) if isinstance(r,dict) else r)
 except Exception as e:print(json.dumps({"ok":False,"error":str(e)},ensure_ascii=False));raise SystemExit(1)
