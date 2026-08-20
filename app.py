from flask import Flask,jsonify,send_from_directory,make_response,request
from pathlib import Path
import threading,time
from engine import load_today_board,scan_selected
app=Flask(__name__);BASE=Path(__file__).parent;lock=threading.Lock();stop_event=threading.Event()
state={"running":False,"phase":"idle","current":"","detail":"","venues_info":[],"matches":[],"errors":[],"counters":{}}
def prog(p):
 with lock:
  state.update({k:v for k,v in p.items() if k!="counters"});state["counters"]=p.get("counters",state.get("counters",{}))
def board_job():
 try:
  r=load_today_board(prog,stop_event)
  with lock:state.update({"venues_info":r["venues_info"],"counters":r["counters"],"phase":"select","current":"開催場を選択"})
 except Exception as e:
  with lock:state.update({"phase":"error","errors":[f"{type(e).__name__}: {e}"]})
 finally:
  with lock:state["running"]=False
def search_job(sel):
 try:
  with lock:b=list(state["venues_info"])
  r=scan_selected(sel,b,prog,stop_event)
  with lock:state.update({"matches":r["matches"],"errors":r["errors"],"counters":r["counters"],"phase":"stopped" if r["stopped"] else "done","current":"途中停止" if r["stopped"] else "検索完了"})
 except Exception as e:
  with lock:state.update({"phase":"error","errors":[f"{type(e).__name__}: {e}"]})
 finally:
  with lock:state["running"]=False
@app.get("/")
def index():return send_from_directory(BASE,"index.html")
@app.get("/api/status")
def status():
 with lock:return jsonify(dict(state))
@app.post("/api/load-venues")
def load():
 with lock:
  if state["running"]:return jsonify(ok=False,reason="running")
  state.update({"running":True,"phase":"board","current":"開催場取得中","detail":"","venues_info":[],"matches":[],"errors":[],"counters":{}});stop_event.clear()
 threading.Thread(target=board_job,daemon=True).start();return jsonify(ok=True)
@app.post("/api/search-selected")
def search():
 sel=(request.get_json(silent=True) or {}).get("selected") or []
 with lock:
  if state["running"]:return jsonify(ok=False,reason="running")
  valid={x["slug"] for x in state["venues_info"]};sel=[x for x in sel if x in valid]
  if not sel:return jsonify(ok=False,reason="no_selection")
  state.update({"running":True,"phase":"races","current":"検索準備中","matches":[],"errors":[]});stop_event.clear()
 threading.Thread(target=search_job,args=(sel,),daemon=True).start();return jsonify(ok=True)
@app.post("/api/stop")
def stop():stop_event.set();return jsonify(ok=True)
@app.post("/api/reset")
def reset():
 with lock:
  if state["running"]:return jsonify(ok=False)
  state.update({"running":False,"phase":"idle","current":"","detail":"","venues_info":[],"matches":[],"errors":[],"counters":{}})
 return jsonify(ok=True)
@app.get("/health")
def health():return jsonify(ok=True,version="select-engine-v1.0-single-page")
if __name__=="__main__":
 import os;app.run(host="0.0.0.0",port=int(os.environ.get("PORT","10000")))
