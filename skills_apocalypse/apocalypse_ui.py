#!/usr/bin/env python3
"""Standalone launcher for Apocalypse Spatial OS.

First-run configuration is owned by the web UI. The CLI only controls the local
server, opens the UI, and optionally refreshes cached analysis.
"""
from __future__ import annotations

import argparse,json,os,signal,socket,subprocess,sys,time,urllib.request,webbrowser
from pathlib import Path

PORT=7749;HOST="127.0.0.1";URL=f"http://localhost:{PORT}"
DATA_DIR=Path.home()/".claude"/"apocalypse";PID_FILE=DATA_DIR/"server.pid";LOG_FILE=DATA_DIR/"server.log";SETUP_FILE=DATA_DIR/"setup.json"
APP_DIR=Path(__file__).resolve().parent;SERVER=APP_DIR/"spatial_server_plus.py";OPS_ANALYSIS=APP_DIR/"ops_analysis.py"


def _http_ok(path,timeout=.8):
 try:
  with urllib.request.urlopen(URL+path,timeout=timeout) as r:return 200<=r.status<300
 except Exception:return False

def _port_open():
 try:
  with socket.create_connection((HOST,PORT),timeout=.5):return True
 except OSError:return False

def _read_pid():
 try:return int(PID_FILE.read_text(encoding="utf-8").strip())
 except Exception:return None

def _pid_alive(pid):
 if not pid:return False
 try:os.kill(pid,0);return True
 except OSError:return False
 except Exception:return _port_open()

def _owned():
 pid=_read_pid();return _pid_alive(pid),pid

def _running():return _http_ok("/api/world")

def status_payload():
 owned,pid=_owned();return {"running":_running(),"pid":pid if owned else None,"port":PORT,"url":URL,"owned_process":owned,"port_open":_port_open(),"initialized":SETUP_FILE.exists(),"log":str(LOG_FILE)}
def print_status():
 info=status_payload();print(json.dumps(info,ensure_ascii=False,indent=2));return 0 if info["running"] else 1

def _terminate(pid):
 try:
  if os.name=="nt":subprocess.run(["taskkill","/PID",str(pid),"/T"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
  else:os.kill(pid,signal.SIGTERM)
 except Exception:pass

def stop_server(quiet=False):
 owned,pid=_owned()
 if not owned or not pid:
  if PID_FILE.exists() and not _port_open():
   try:PID_FILE.unlink()
   except OSError:pass
  if not quiet:print("Apocalypse is not running as an owned process.")
  return not _port_open()
 _terminate(pid);deadline=time.time()+4
 while time.time()<deadline:
  if not _pid_alive(pid) and not _port_open():break
  time.sleep(.1)
 if _pid_alive(pid) and os.name=="nt":subprocess.run(["taskkill","/PID",str(pid),"/T","/F"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
 try:
  if PID_FILE.exists() and _read_pid()==pid:PID_FILE.unlink()
 except OSError:pass
 ok=not _port_open()
 if not quiet:print("Apocalypse stopped." if ok else f"Port {PORT} is still in use.")
 return ok

def _spawn():
 if not SERVER.exists():raise FileNotFoundError(f"Spatial runtime missing: {SERVER}")
 DATA_DIR.mkdir(parents=True,exist_ok=True);log=open(LOG_FILE,"ab",buffering=0);kw={"cwd":str(APP_DIR),"stdin":subprocess.DEVNULL,"stdout":log,"stderr":subprocess.STDOUT,"close_fds":True}
 if os.name=="nt":kw["creationflags"]=getattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0)|getattr(subprocess,"DETACHED_PROCESS",0)|getattr(subprocess,"CREATE_NO_WINDOW",0)
 else:kw["start_new_session"]=True
 try:return subprocess.Popen([sys.executable,str(SERVER)],**kw)
 finally:log.close()

def start_server(open_browser=True,onboarding=False):
 if _running():
  target=URL+("/?onboarding=1" if onboarding else "");print(f"Apocalypse already running → {target}")
  if open_browser:webbrowser.open(target)
  return True
 if _port_open():print(f"Port {PORT} is already occupied. Apocalypse will not terminate an unrelated process.",file=sys.stderr);return False
 proc=_spawn();deadline=time.time()+6
 while time.time()<deadline:
  if _running():
   target=URL+("/?onboarding=1" if onboarding else "");print(f"Apocalypse started (pid {proc.pid}) → {target}")
   if open_browser:webbrowser.open(target)
   return True
  if proc.poll() is not None:break
  time.sleep(.12)
 print(f"Apocalypse failed to start. Check {LOG_FILE}",file=sys.stderr);return False

def open_ui(onboarding=False):
 if not _running():return start_server(True,onboarding)
 target=URL+("/?onboarding=1" if onboarding else "");webbrowser.open(target);print(f"Opened {target}");return True

def run_analysis(hours=24):
 if not OPS_ANALYSIS.exists():print("ops_analysis.py is missing.",file=sys.stderr);return 1
 return subprocess.run([sys.executable,str(OPS_ANALYSIS),"--hours",str(hours)],cwd=str(APP_DIR)).returncode

def main(argv=None):
 p=argparse.ArgumentParser(description="Apocalypse Spatial OS launcher")
 p.add_argument("action",nargs="?",default="start",choices=("start","stop","restart","status","open","init","analyze"));p.add_argument("--no-open",action="store_true");p.add_argument("--hours",type=int,default=24);a=p.parse_args(argv)
 if a.action=="status":return print_status()
 if a.action=="stop":return 0 if stop_server() else 1
 if a.action=="open":return 0 if open_ui() else 1
 if a.action=="init":return 0 if open_ui(onboarding=True) else 1
 if a.action=="analyze":return run_analysis(max(1,a.hours))
 if a.action=="restart":
  if _owned()[0]:stop_server(True)
  elif _port_open():print(f"Port {PORT} is occupied; restart aborted.",file=sys.stderr);return 1
  return 0 if start_server(not a.no_open) else 1
 return 0 if start_server(not a.no_open) else 1

if __name__=="__main__":raise SystemExit(main())
