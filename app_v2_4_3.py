# -*- coding: utf-8 -*-
from flask import Flask, jsonify, send_from_directory, make_response
from pathlib import Path
import threading
import time
import os

from engine import scan_today

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
APP_VERSION = "2.4.3-worker-stable"

lock = threading.Lock()
scan_thread = None
state = {
    "running": False,
    "phase": "idle",
    "current": "",
    "done": 0,
    "total": 0,
    "last_update": 0,
    "date": "",
    "matches": [],
    "unpublished": [],
    "checked_races": 0,
    "errors": [],
    "message": ""
}

CACHE_SECONDS = 600



def no_cache(response):
    """ブラウザ/Service Workerに古い画面やAPI結果を残さない。"""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

def update_progress(p):
    with lock:
        state["phase"] = p.get("phase", state["phase"])
        state["current"] = p.get("current", state["current"])
        state["done"] = p.get("done", state["done"])
        state["total"] = p.get("total", state["total"])



def run_scan():
    global scan_thread

    with lock:
        if state["running"]:
            return
        state.update({
            "running": True,
            "phase": "starting",
            "current": "準備中",
            "done": 0,
            "total": 0,
            "message": ""
        })

    try:
        result = scan_today(update_progress)

        with lock:
            state["date"] = result.get("date", "")
            state["matches"] = result.get("matches", [])
            state["unpublished"] = result.get("unpublished", [])
            state["checked_races"] = result.get("checked_races", 0)
            state["errors"] = result.get("errors", [])
            state["last_update"] = int(time.time())
            state["phase"] = "done"
            state["current"] = "完了"
            state["message"] = f"該当 {len(state['matches'])}件"

    except Exception as e:
        with lock:
            state["phase"] = "error"
            state["message"] = f"{type(e).__name__}: {e}"

    finally:
        with lock:
            state["running"] = False
        scan_thread = None


def start_scan(force=False):
    """
    HTTPリクエスト本体では検索を実行しない。
    daemon thread に即時退避し、POSTはすぐ返す。
    """
    global scan_thread

    with lock:
        running = state["running"]
        fresh = bool(
            state["last_update"]
            and (time.time() - state["last_update"] < CACHE_SECONDS)
        )

    if running:
        return False, "running"

    if fresh and not force:
        return False, "fresh"

    # 既存threadが生きていたら二重起動しない
    if scan_thread is not None and scan_thread.is_alive():
        return False, "running"

    scan_thread = threading.Thread(
        target=run_scan,
        name="winticket-scan",
        daemon=True
    )
    scan_thread.start()

    return True, "started"


@app.route("/")
def index():
    response = make_response(send_from_directory(BASE_DIR, "index.html"))
    return no_cache(response)


@app.route("/manifest.webmanifest")
def manifest():
    response = make_response(send_from_directory(BASE_DIR, "manifest.webmanifest"))
    return no_cache(response)


@app.route("/sw.js")
def sw():
    response = make_response(send_from_directory(BASE_DIR, "sw.js"))
    response.headers["Service-Worker-Allowed"] = "/"
    return no_cache(response)


@app.route("/health")
def health():
    return no_cache(jsonify({"ok": True, "running": state["running"], "version": APP_VERSION}))


@app.route("/api/status")
def status():
    with lock:
        data = dict(state)
        data["version"] = APP_VERSION
    return no_cache(jsonify(data))


@app.route("/api/search", methods=["POST"])
def search():
    started, reason = start_scan(force=True)
    return no_cache(jsonify({"started": started, "reason": reason}))


@app.route("/api/auto", methods=["POST"])
def auto():
    started, reason = start_scan(force=False)
    return no_cache(jsonify({"started": started, "reason": reason}))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
