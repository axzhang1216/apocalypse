#!/usr/bin/env python3
"""Desktop lifecycle helpers for Apocalypse Spatial OS."""
from __future__ import annotations

import json,os,re,subprocess,sys,tempfile,urllib.error,urllib.request
from pathlib import Path
from typing import Any

APP_VERSION="0.3.0";REPO="axzhang1216/apocalypse";RELEASE_API=f"https://api.github.com/repos/{REPO}/releases/latest"

def _version_tuple(v):return tuple(int(x) for x in re.findall(r"\d+",str(v).split("-",1)[0])[:4]) or (0,)
def _arch():return "x64" if sys.maxsize>2**32 else "x86"
def _github_json(url,timeout=8.0):
 req=urllib.request.Request(url,headers={"accept":"application/vnd.github+json","user-agent":f"Apocalypse/{APP_VERSION}","x-github-api-version":"2022-11-28"})
 with urllib.request.urlopen(req,timeout=timeout) as r:return json.loads(r.read().decode("utf-8",errors="replace"))
def app_status():return {"version":APP_VERSION,"arch":_arch(),"platform":sys.platform,"packaged":bool(getattr(sys,"frozen",False)),"reinitialize_supported":True,"self_update_supported":os.name=="nt"}

def check_update():
 try:release=_github_json(RELEASE_API)
 except urllib.error.HTTPError as e:raise RuntimeError(f"GitHub update check failed: HTTP {e.code}") from e
 except Exception as e:raise RuntimeError(f"GitHub update check failed: {e}") from e
 tag=str(release.get("tag_name") or "").strip();latest=tag[1:] if tag.lower().startswith("v") else tag;arch=_arch();expected=f"Apocalypse-Setup-{arch}.exe".lower();asset=next((a for a in release.get("assets") or [] if str(a.get("name") or "").lower()==expected),None);available=bool(latest and _version_tuple(latest)>_version_tuple(APP_VERSION))
 return {"current_version":APP_VERSION,"latest_version":latest or None,"tag":tag or None,"available":available,"arch":arch,"asset_name":asset.get("name") if isinstance(asset,dict) else None,"asset_url":asset.get("browser_download_url") if isinstance(asset,dict) else None,"asset_size":asset.get("size") if isinstance(asset,dict) else None,"release_url":release.get("html_url"),"published_at":release.get("published_at"),"can_install":bool(os.name=="nt" and asset and asset.get("browser_download_url"))}

def _download(url,dest):
 req=urllib.request.Request(url,headers={"user-agent":f"Apocalypse/{APP_VERSION}"})
 with urllib.request.urlopen(req,timeout=30) as r,open(dest,"wb") as f:
  while True:
   chunk=r.read(1024*1024)
   if not chunk:break
   f.write(chunk)
  f.flush();os.fsync(f.fileno())

def apply_update():
 if os.name!="nt":raise RuntimeError("Automatic installer updates are currently supported on Windows only.")
 info=check_update()
 if not info.get("available"):return {"ok":True,"launched":False,**info,"message":"Apocalypse is already up to date."}
 if not info.get("asset_url"):raise RuntimeError(f"Release {info.get('tag') or ''} has no {info['arch']} installer asset.")
 d=Path(tempfile.gettempdir())/"Apocalypse"/"updates"/str(info.get("tag") or "latest");d.mkdir(parents=True,exist_ok=True);installer=d/str(info.get("asset_name") or f"Apocalypse-Setup-{info['arch']}.exe");tmp=installer.with_suffix(installer.suffix+".download")
 try:
  _download(str(info["asset_url"]),tmp)
  if tmp.stat().st_size<1_000_000:raise RuntimeError("Downloaded installer is unexpectedly small.")
  os.replace(tmp,installer)
 finally:
  try:tmp.unlink(missing_ok=True)
  except Exception:pass
 flags=getattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0) if os.name=="nt" else 0;proc=subprocess.Popen([str(installer)],cwd=str(installer.parent),creationflags=flags)
 return {"ok":True,"launched":True,**info,"installer":str(installer),"pid":proc.pid,"message":"Update installer launched. Complete the installer to apply the update."}
