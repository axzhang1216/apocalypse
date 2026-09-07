#!/usr/bin/env python3
"""Windows desktop shell for Apocalypse Spatial OS."""
from __future__ import annotations
import os,sys,threading,time,urllib.request
from pathlib import Path
import webview

BASE=Path(__file__).resolve().parent
if str(BASE) not in sys.path:sys.path.insert(0,str(BASE))
import server as core
import spatial_server_plus as spatial
URL=f"http://localhost:{spatial.PORT}"

def _alive(timeout=.7):
 try:
  with urllib.request.urlopen(URL+"/api/world",timeout=timeout) as r:return 200<=r.status<300
 except Exception:return False

def _port_occupied():
 try:
  with urllib.request.urlopen(URL+"/api/settings/status",timeout=.5) as r:return 200<=r.status<300
 except Exception:return False

def _start_server():
 if _alive():return None
 if _port_occupied():raise RuntimeError("Another Apocalypse process is already using port 7749. Close it, then reopen Apocalypse.")
 core.DATA_DIR.mkdir(parents=True,exist_ok=True);core.SESSIONS_DIR.mkdir(parents=True,exist_ok=True);pid=core.DATA_DIR/"server.pid";pid.write_text(str(os.getpid()),encoding="utf-8");threading.Thread(target=core.broadcast_thread,daemon=True).start();srv=spatial.http.server.ThreadingHTTPServer(("127.0.0.1",spatial.PORT),spatial.Handler);threading.Thread(target=srv.serve_forever,daemon=True,name="apocalypse-http").start();deadline=time.time()+5
 while time.time()<deadline:
  if _alive():return srv
  time.sleep(.08)
 srv.shutdown();raise RuntimeError("Apocalypse Spatial OS failed to start on port 7749")
def _cleanup(srv):
 if srv is None:return
 try:srv.shutdown();srv.server_close()
 except Exception:pass
 try:
  p=core.DATA_DIR/"server.pid"
  if p.exists() and p.read_text(encoding="utf-8").strip()==str(os.getpid()):p.unlink()
 except Exception:pass

def main():
 try:srv=_start_server()
 except Exception as e:
  webview.create_window("Apocalypse — Startup Error",html=f"<body style='background:#17191D;color:#E6E2DA;font-family:Segoe UI;padding:28px'><h2>Apocalypse could not start</h2><pre style='white-space:pre-wrap;color:#F3A1BD'>{str(e)}</pre></body>",width=720,height=360,resizable=False);webview.start(debug=False);return 1
 window=webview.create_window("Apocalypse",URL,width=1600,height=1000,min_size=(1100,700),background_color="#17191D",resizable=True,text_select=True)
 def after(win):
  try:win.maximize()
  except Exception:pass
 try:webview.start(after,window,gui="edgechromium",debug=False,private_mode=False)
 finally:_cleanup(srv)
 return 0
if __name__=="__main__":raise SystemExit(main())
