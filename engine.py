# -*- coding: utf-8 -*-
import re
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://www.winticket.jp"
ENGINE_VERSION = "mobile-select-v3.0-public-only"
TARGET_STAR = 2
TARGET_LINES = {"3.2.2", "2.3.2", "2.2.3"}
TARGET_ORDERS = {"◎○△", "◎○×"}
JST = __import__("datetime").timezone(timedelta(hours=9))

VENUES = {
    "函館":"hakodate","青森":"aomori","いわき平":"iwakitaira","弥彦":"yahiko","前橋":"maebashi",
    "取手":"toride","宇都宮":"utsunomiya","大宮":"omiya","西武園":"seibuen","京王閣":"keiokaku",
    "立川":"tachikawa","松戸":"matsudo","千葉":"chiba","川崎":"kawasaki","平塚":"hiratsuka",
    "小田原":"odawara","伊東":"ito","静岡":"shizuoka","名古屋":"nagoya","岐阜":"gifu",
    "大垣":"ogaki","豊橋":"toyohashi","富山":"toyama","松阪":"matsusaka","四日市":"yokkaichi",
    "福井":"fukui","奈良":"nara","向日町":"mukomachi","和歌山":"wakayama","岸和田":"kishiwada",
    "玉野":"tamano","広島":"hiroshima","防府":"hofu","高松":"takamatsu","小松島":"komatsushima",
    "高知":"kochi","松山":"matsuyama","小倉":"kokura","久留米":"kurume","武雄":"takeo",
    "佐世保":"sasebo","別府":"beppu","熊本":"kumamoto"
}
REVERSE = {v: k for k, v in VENUES.items()}


class StopRequested(Exception):
    pass


def _today_jst():
    return datetime.now(JST).date()


def _check_stop(stop_event):
    if stop_event and stop_event.is_set():
        raise StopRequested()


def _browser_args():
    return [
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions",
        "--disable-background-networking", "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
        "--disable-default-apps", "--disable-sync", "--metrics-recording-only", "--no-first-run",
        "--no-default-browser-check", "--mute-audio",
        "--disable-features=Translate,BackForwardCache,MediaRouter,OptimizationHints",
    ]


def enable_fast_mode(context):
    blocked = ("doubleclick", "googletagmanager", "google-analytics", "adservice", "adsystem", "facebook.net", "clarity.ms")
    def handler(route):
        req = route.request
        url = req.url.lower()
        if req.resource_type in ("image", "media", "font") or any(k in url for k in blocked):
            route.abort()
        else:
            route.continue_()
    context.route("**/*", handler)


def _open_session(p):
    browser = p.chromium.launch(headless=True, args=_browser_args())
    context = browser.new_context(locale="ja-JP", viewport={"width": 1000, "height": 850}, service_workers="block")
    enable_fast_mode(context)
    page = context.new_page()
    page.set_default_timeout(6000)
    return browser, context, page


def _close_session(browser=None, context=None, page=None):
    for obj in (page, context, browser):
        try:
            if obj:
                obj.close()
        except Exception:
            pass


def _body_text(page):
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def goto(page, url, stop_event=None):
    _check_stop(stop_event)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=8000)
    except Exception:
        pass
    _check_stop(stop_event)
    page.wait_for_timeout(180)
    body = _body_text(page)
    if not body:
        raise RuntimeError(f"ページ本文を取得できません: {url}")
    return body


def _session_from_times(text):
    text = text or ""
    if "ミッドナイト" in text: return "MN"
    if "ナイター" in text: return "N"
    if "モーニング" in text: return "M"
    times = [int(h) * 60 + int(m) for h, m in re.findall(r"発走\s*([0-2]?\d):([0-5]\d)", text)]
    if not times:
        times = [int(h) * 60 + int(m) for h, m in re.findall(r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)", text)]
    if times:
        lo, hi = min(times), max(times)
        if hi >= 21 * 60: return "MN"
        if lo >= 14 * 60 or hi >= 18 * 60: return "N"
        if lo <= 10 * 60 + 30 and hi <= 16 * 60: return "M"
    return "D"


def _grade_from_racecard(text):
    """公開出走表のレース級から開催グレードを判定する。"""
    text = text or ""
    # G開催はF1扱いにしない。F2選択対象外として明示。
    if re.search(r"\bG[123]\b|共同通信社杯|オールスター|記念競輪", text):
        return "G"
    has_s = bool(re.search(r"S級(?:一般|選抜|特選|準決勝|決勝|予選)", text))
    has_a = bool(re.search(r"A級(?:一般|特選|準決勝|決勝|予選|チャレンジ)", text))
    has_l = bool(re.search(r"L級|ガールズ", text))
    if has_s and has_a:
        return "F1"
    if has_a or has_l:
        return "F2"
    return "?"


def _public_get(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
        "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.5",
    }
    r = requests.get(url, headers=headers, timeout=12)
    r.raise_for_status()
    return r.text


def _today_event_links_public(today):
    """
    ログイン不要の「競輪 開催日程」公開ページから候補イベントを抽出する。
    各イベントの公開出走表を確認し、ページ内に今日の日付があるものだけ残す。
    """
    html = _public_get(f"{BASE}/keirin/schedules/")
    soup = BeautifulSoup(html, "html.parser")
    candidates = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(BASE, a.get("href", ""))
        m = re.search(r"/keirin/([^/]+)/racecard/(\d{8,})", href)
        if not m:
            continue
        slug, sid = m.group(1), m.group(2)
        if slug not in REVERSE:
            continue
        try:
            start = datetime.strptime(sid[:8], "%Y%m%d").date()
        except Exception:
            continue
        delta = (today - start).days
        # 通常開催3日/G開催4日を考慮。候補を広めに取り、次段で本文日付確認。
        if 0 <= delta <= 4:
            candidates[(slug, sid)] = href

    out = []
    today_markers = {
        f"{today.year}年{today.month}月{today.day}日",
        f"{today.month}月{today.day}日",
    }
    for (slug, sid), href in candidates.items():
        url = f"{BASE}/keirin/{slug}/racecard/{sid}/races"
        try:
            event_html = _public_get(url)
            event_soup = BeautifulSoup(event_html, "html.parser")
            text = event_soup.get_text(" ", strip=True)
            title = event_soup.title.get_text(" ", strip=True) if event_soup.title else ""
            if not any(x in (title + " " + text[:2500]) for x in today_markers):
                continue
            start = datetime.strptime(sid[:8], "%Y%m%d").date()
            day = (today - start).days + 1
            out.append({
                "venue": REVERSE[slug], "slug": slug, "sid": sid, "day": day,
                "racecard_url": url, "_text": text,
            })
        except Exception:
            continue
    return out


def discover_today_board(page, today, stop_event):
    """ログイン不要の公開開催日程＋公開出走表だけで今日の一覧を作る。"""
    _check_stop(stop_event)
    items = _today_event_links_public(today)
    if not items:
        raise RuntimeError("ログイン不要のWINTICKET公開開催日程から本日の開催場を取得できません")

    out = []
    for item in items:
        _check_stop(stop_event)
        text = item.pop("_text", "")
        grade = _grade_from_racecard(text)
        session = _session_from_times(text)
        races = sorted({int(x) for x in re.findall(r"(?<!\d)(\d{1,2})R(?:\s|$)", text) if 1 <= int(x) <= 12})
        if not races:
            races = list(range(1, 13))
        out.append({**item, "grade": grade, "session": session, "races": races})

    # 安定した表示順：モーニング→デイ→ナイター→ミッドナイト、同区分は場名。
    rank = {"M": 0, "D": 1, "N": 2, "MN": 3, "?": 4}
    out.sort(key=lambda x: (rank.get(x.get("session", "?"), 4), x.get("venue", "")))
    return out

def confidence_dom(page):
    loc = page.locator('[aria-label*="3点中"]')
    for i in range(min(loc.count(), 12)):
        try:
            label = loc.nth(i).get_attribute("aria-label") or ""
        except Exception:
            continue
        m = re.search(r"3点中\s*([0-3])点", label)
        if m:
            return int(m.group(1)), label
    try:
        html = page.content()
        m = re.search(r'aria-label=["\']3点中\s*([0-3])点["\']', html)
        if m:
            return int(m.group(1)), m.group(0)
    except Exception:
        pass
    return None, ""


def ancestor_texts(page, label, levels=7):
    out = []
    try:
        loc = page.get_by_text(label, exact=True)
        if loc.count() == 0:
            loc = page.get_by_text(re.compile(re.escape(label)))
        for i in range(min(loc.count(), 5)):
            vals = loc.nth(i).evaluate(f"""e=>{{const out=[];let n=e;for(let k=0;k<{levels}&&n;k++,n=n.parentElement){{const t=(n.innerText||'').trim();if(t)out.push(t);}}return out;}}""")
            for v in vals:
                if v and v not in out:
                    out.append(v)
    except Exception:
        pass
    return out


def extract_ai_number(page, label):
    for txt in ancestor_texts(page, label):
        nums = re.findall(r"(?<!\d)([1-9])(?!\d)", txt.replace(label, " "))
        if len(nums) == 1 and len(txt) <= 160:
            return int(nums[0])
    for txt in ancestor_texts(page, label):
        nums = re.findall(r"(?<!\d)([1-9])(?!\d)", txt.replace(label, " "))
        if nums and len(txt) <= 260:
            return int(nums[0])
    return ""


def extract_ai_marks(page):
    return (extract_ai_number(page, "本命"), extract_ai_number(page, "対抗"), extract_ai_number(page, "単穴"), extract_ai_number(page, "連下"))


def mark_for_rider(r, hon, tai, ana, ren):
    if r == hon: return "◎"
    if r == tai: return "○"
    if r == ana: return "△"
    if r == ren: return "×"
    return "他"


def line_mark_order(groups, hon, tai, ana, ren):
    return ".".join("".join(mark_for_rider(r, hon, tai, ana, ren) for r in g) for g in groups)


def three_line_order(groups, hon, tai, ana, ren):
    for g in groups:
        if len(g) == 3:
            return "".join(mark_for_rider(r, hon, tai, ana, ren) for r in g)
    return ""


def _collect_riders(page):
    try:
        vals = page.evaluate("""()=>{const out=[],seen=new Set();for(const sel of ['[class*="RaceCard"] [class*="Bib"]','[class*="Racer"] [class*="Bib"]','[class*="Bib"]']){for(const e of document.querySelectorAll(sel)){const cls=typeof e.className==='string'?e.className:'';if(cls.includes('LinePowerBib'))continue;const t=(e.innerText||'').trim();if(/^[1-9]$/.test(t)&&!seen.has(t)){seen.add(t);out.push(Number(t));}}if(out.length>=5)break;}return out;}""")
        return vals if 5 <= len(vals) <= 9 else []
    except Exception:
        return []


def extract_line(page, stop_event=None):
    _check_stop(stop_event)
    all_riders = _collect_riders(page)
    formed = []
    for attempt in range(2):
        try:
            heading = page.get_by_text(re.compile("ラインパワー比較"))
            if heading.count():
                heading.first.scroll_into_view_if_needed(timeout=1800)
                page.wait_for_timeout(250 + 250 * attempt)
                data = heading.first.evaluate("""e=>{let root=e;for(let k=0;k<10&&root;k++,root=root.parentElement){const groups=[];for(const li of root.querySelectorAll('li')){const nums=[];for(const b of li.querySelectorAll('[class*="LinePowerBib"]')){const t=(b.innerText||'').trim();if(/^[1-9]$/.test(t)&&!nums.includes(Number(t)))nums.push(Number(t));}if(nums.length>=2)groups.push(nums);}if(groups.length)return groups;}return [];}""")
                if data:
                    formed = data
                    break
        except Exception:
            pass
        try:
            page.evaluate("window.scrollBy(0,700)")
            page.wait_for_timeout(250)
        except Exception:
            pass
    if not formed:
        return "", []
    used = []
    for g in formed:
        for n in g:
            if n not in used: used.append(n)
    groups = list(formed)
    if all_riders:
        groups += [[n] for n in all_riders if n not in used]
    counts = [len(g) for g in groups]
    if 4 <= sum(counts) <= 9 and len(groups) >= 2:
        return ".".join(map(str, counts)), groups
    return "", []


def _progress(progress, counters, **kw):
    kw["counters"] = dict(counters)
    progress(kw)


def load_today_board(progress=None, stop_event=None):
    progress = progress or (lambda x: None)
    today = _today_jst()
    c = {"venues_found": 0, "errors": 0}
    browser = context = page = None
    try:
        with sync_playwright() as p:
            try:
                browser, context, page = _open_session(p)
                _progress(progress, c, phase="board", current="今日の開催場を取得中", detail="ログイン不要のWINTICKET公開ページを確認")
                board = discover_today_board(page, today, stop_event)
                c["venues_found"] = len(board)
                _progress(progress, c, phase="select", current="開催場を選択", detail="F2の開催場にチェックしてください", venues_info=board)
                return {"venues_info": board, "counters": c, "errors": [], "stopped": False}
            finally:
                _close_session(browser, context, page)
    except StopRequested:
        return {"venues_info": [], "counters": c, "errors": [], "stopped": True}


def scan_selected(selected, board, progress=None, stop_event=None):
    progress = progress or (lambda x: None)
    selected = set(selected or [])
    jobs = [x for x in (board or []) if x.get("slug") in selected]
    c = {"venues_found": len(board or []), "selected_venues": len(jobs), "checked_races": 0, "unpublished": 0,
         "star2": 0, "line_target": 0, "order_target": 0, "matched": 0, "errors": 0}
    result = {"matches": [], "errors": [], "counters": c, "venues_info": board or [], "stopped": False}
    try:
        with sync_playwright() as p:
            for vi, info in enumerate(jobs, 1):
                _check_stop(stop_event)
                venue, slug = info["venue"], info["slug"]
                # 安全側：一覧でF1/Gと分かっている場は検索しない。
                if info.get("grade") not in ("F2", "?"):
                    continue
                browser = context = page = None
                try:
                    browser, context, page = _open_session(p)
                    races = info.get("races") or list(range(1, 13))
                    for race in races:
                        _check_stop(stop_event)
                        url = f"{BASE}/keirin/{slug}/predictions/{info['sid']}/{info['day']}/{race}"
                        _progress(progress, c, phase="races", current=f"{venue} {race}R", detail=f"選択{len(jobs)}場中 {vi}場目", matches=list(result["matches"]), venues_info=board)
                        try:
                            goto(page, url, stop_event)
                            c["checked_races"] += 1
                            star, _ = confidence_dom(page)
                            if star is None:
                                page.wait_for_timeout(250)
                                star, _ = confidence_dom(page)
                            if star is None:
                                c["unpublished"] += 1
                                continue
                            if star != TARGET_STAR:
                                continue
                            c["star2"] += 1
                            line, groups = extract_line(page, stop_event)
                            if line not in TARGET_LINES:
                                continue
                            c["line_target"] += 1
                            hon, tai, ana, ren = extract_ai_marks(page)
                            if hon in ("", None) or tai in ("", None):
                                c["unpublished"] += 1
                                continue
                            order3 = three_line_order(groups, hon, tai, ana, ren)
                            if order3 not in TARGET_ORDERS:
                                continue
                            c["order_target"] += 1
                            full_order = line_mark_order(groups, hon, tai, ana, ren)
                            result["matches"].append({
                                "venue": venue, "slug": slug, "race": race, "grade": "F2",
                                "session": info.get("session", "?"), "star": "星2", "line": line,
                                "three_order": order3, "order": full_order, "prediction_url": url,
                                "winticket_url": url
                            })
                            c["matched"] += 1
                        except StopRequested:
                            raise
                        except Exception as e:
                            c["errors"] += 1
                            result["errors"].append(f"{venue} {race}R: {type(e).__name__}: {e}")
                        _progress(progress, c, phase="races", current=f"{venue} {race}R", detail="判定中", matches=list(result["matches"]), venues_info=board)
                finally:
                    _close_session(browser, context, page)
        return result
    except StopRequested:
        result["stopped"] = True
        return result
