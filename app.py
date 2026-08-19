# -*- coding: utf-8 -*-
from flask import Flask, jsonify, send_from_directory, make_response, Response
from pathlib import Path
import threading
import time

from engine import scan_today

APP_VERSION = "mobile-new-v1.1"
BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
lock = threading.Lock()
stop_event = threading.Event()

EMPTY_COUNTERS = {
    "venues_found":0,"f2_venues":0,"f1_venues":0,"other_venues":0,
    "checked_races":0,"girls_l":0,"non_f2_races":0,"f2_races":0,
    "star2":0,"line_target":0,"order_target":0,"matched":0,"errors":0,
}

state = {
    "running":False,
    "phase":"idle",
    "current":"",
    "detail":"",
    "last_update":0,
    "matches":[],
    "errors":[],
    "skipped":[],
    "counters":dict(EMPTY_COUNTERS),
    "message":"",
}


def _no_cache(r):
    r.headers["Cache-Control"]="no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"]="no-cache"
    r.headers["Expires"]="0"
    return r


def progress_update(p):
    with lock:
        state["phase"]=p.get("phase",state["phase"])
        state["current"]=p.get("current",state["current"])
        state["detail"]=p.get("detail",state["detail"])


def run_scan():
    with lock:
        if state["running"]:
            return
        state.update({
            "running":True,
            "phase":"starting",
            "current":"検索準備中",
            "detail":"",
            "last_update":0,
            "matches":[],
            "errors":[],
            "skipped":[],
            "counters":dict(EMPTY_COUNTERS),
            "message":"",
        })
        stop_event.clear()

    try:
        result=scan_today(progress_update,stop_event)
        with lock:
            state["matches"]=result.get("matches",[])
            state["errors"]=result.get("errors",[])
            state["skipped"]=result.get("skipped",[])
            state["counters"]=result.get("counters",dict(EMPTY_COUNTERS))
            state["last_update"]=int(time.time())

            if result.get("stopped"):
                state["phase"]="stopped"
                state["current"]="途中停止"
                state["message"]="途中停止しました。終了でリセットできます。"
            else:
                state["phase"]="done"
                state["current"]="検索完了"
                state["message"]=f"該当 {len(state['matches'])}件"

    except Exception as e:
        with lock:
            state["phase"]="error"
            state["current"]="エラー"
            state["message"]=f"{type(e).__name__}: {e}"
            state["errors"].append(state["message"])
    finally:
        with lock:
            state["running"]=False


@app.route("/")
def index():
    return _no_cache(make_response(send_from_directory(BASE_DIR,"index.html")))


@app.route("/api/status")
def api_status():
    with lock:
        data=dict(state)
    data["app_version"]=APP_VERSION
    return _no_cache(jsonify(data))


@app.route("/api/start",methods=["POST"])
def api_start():
    with lock:
        if state["running"]:
            return _no_cache(jsonify({"ok":False,"reason":"running"}))
    threading.Thread(target=run_scan,daemon=True,name="winticket-search").start()
    return _no_cache(jsonify({"ok":True}))


@app.route("/api/stop",methods=["POST"])
def api_stop():
    with lock:
        running=state["running"]
    if running:
        stop_event.set()
    return _no_cache(jsonify({"ok":True,"running":running}))


@app.route("/api/reset",methods=["POST"])
def api_reset():
    with lock:
        if state["running"]:
            return _no_cache(jsonify({"ok":False,"reason":"running"}))
        state.update({
            "running":False,
            "phase":"idle",
            "current":"",
            "detail":"",
            "last_update":0,
            "matches":[],
            "errors":[],
            "skipped":[],
            "counters":dict(EMPTY_COUNTERS),
            "message":"",
        })
        stop_event.clear()
    return _no_cache(jsonify({"ok":True}))


@app.route("/health")
def health():
    with lock:
        running=state["running"]
    return jsonify({"ok":True,"running":running,"version":APP_VERSION})


@app.route("/manifest.webmanifest")
def manifest():
    return _no_cache(jsonify({
        "name":"WINTICKET 今日の買い目",
        "short_name":"今日の買い目",
        "start_url":"/",
        "display":"standalone",
        "background_color":"#06101d",
        "theme_color":"#06101d",
    }))


@app.route("/sw.js")
def sw():
    js='self.addEventListener("install",e=>self.skipWaiting());self.addEventListener("activate",e=>{e.waitUntil(self.clients.claim())});'
    r=Response(js,mimetype="application/javascript")
    r.headers["Service-Worker-Allowed"]="/"
    return _no_cache(r)


if __name__=="__main__":
    import os
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","10000")))
