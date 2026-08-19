# -*- coding: utf-8 -*-
import re
import time
import threading
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

BASE = "https://www.winticket.jp"
ENGINE_VERSION = "mobile-new-v1"
TARGET_STAR = 2
TARGET_LINES = {"3.2.2", "2.3.2", "2.2.3"}
TARGET_ORDERS = {"◎○△", "◎○×"}
MAX_RETRIES = 2

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

_BROWSER_READY = False
_BROWSER_LOCK = threading.Lock()


class StopRequested(Exception):
    pass


def _log(msg):
    print(f"[WINTICKET] {msg}", flush=True)


def _today_jst():
    return datetime.now(JST).date()


def _check_stop(stop_event):
    if stop_event and stop_event.is_set():
        raise StopRequested()


def ensure_browser(progress=None):
    """検索時はChromiumの存在確認だけ。インストールはRenderのBuild時に行う。"""
    global _BROWSER_READY
    if _BROWSER_READY:
        return True
    try:
        with sync_playwright() as p:
            exe = p.chromium.executable_path
        if exe and Path(exe).exists():
            _BROWSER_READY = True
            _log(f"BROWSER_OK {exe}")
            return True
    except Exception as e:
        _log(f"BROWSER_CHECK {type(e).__name__}: {e}")
    return False

def _browser_args():
    return [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-default-apps",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-first-run",
        "--no-default-browser-check",
        "--mute-audio",
        "--disable-features=Translate,BackForwardCache,MediaRouter,OptimizationHints",
    ]


def enable_fast_mode(context):
    blocked = (
        "doubleclick", "googletagmanager", "google-analytics",
        "adservice", "adsystem", "facebook.net", "clarity.ms"
    )
    def handler(route):
        req = route.request
        url = req.url.lower()
        if req.resource_type in ("image","media","font") or any(k in url for k in blocked):
            route.abort()
        else:
            route.continue_()
    context.route("**/*", handler)


def goto(page, url, stop_event=None):
    """ページ取得。body待ちで全検索を落とさず、HTML/本文を段階的に回収する。"""
    last = None
    for attempt in range(MAX_RETRIES + 1):
        _check_stop(stop_event)
        try:
            page.goto(url, wait_until="commit", timeout=18000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(250 + attempt * 250)
            try:
                body = page.locator("body")
                if body.count() > 0:
                    txt = body.first.inner_text(timeout=2500)
                    if txt.strip():
                        return txt
            except Exception:
                pass
            # bodyのinner_textが遅い/取れない場合でも、documentElementから救済。
            try:
                txt = page.evaluate("() => document.body?.innerText || document.documentElement?.innerText || ''") or ''
                if txt.strip():
                    return txt
            except Exception:
                pass
            # 最後の救済: HTML自体が取れていれば返す。
            html = page.content()
            if html:
                return html
            raise RuntimeError("ページ本文を取得できませんでした")
        except Exception as e:
            last = e
            if attempt < MAX_RETRIES:
                try:
                    page.wait_for_timeout(500 + attempt * 500)
                except Exception:
                    time.sleep(0.5)
    raise last


def parse_grade(text):
    """F1/F2/Lを拾う。L1/L2/L級/ガールズはL扱い。"""
    if not text:
        return ""
    m = re.search(r"(?<![A-Z0-9])(F1|F2|L1|L2)(?![A-Z0-9])", text)
    if m:
        v = m.group(1)
        return "L" if v.startswith("L") else v
    if "L級" in text or "ガールズ" in text or "女子" in text:
        return "L"
    return ""


def confidence_dom(page):
    loc = page.locator('[aria-label*="3点中"]')
    for i in range(min(loc.count(), 10)):
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
    out=[]
    try:
        loc=page.get_by_text(label, exact=True)
        if loc.count()==0:
            loc=page.get_by_text(re.compile(re.escape(label)))
        for i in range(min(loc.count(),5)):
            vals=loc.nth(i).evaluate(f"""e=>{{
                const out=[];let n=e;
                for(let k=0;k<{levels}&&n;k++,n=n.parentElement){{
                    const t=(n.innerText||'').trim();
                    if(t)out.push(t);
                }}
                return out;
            }}""")
            for v in vals:
                if v and v not in out:
                    out.append(v)
    except Exception:
        pass
    return out


def extract_ai_number(page, label):
    for txt in ancestor_texts(page, label):
        cleaned=txt.replace(label," ")
        nums=re.findall(r"(?<!\d)([1-9])(?!\d)", cleaned)
        if len(nums)==1 and len(txt)<=140:
            return int(nums[0])
    for txt in ancestor_texts(page, label):
        cleaned=txt.replace(label," ")
        nums=re.findall(r"(?<!\d)([1-9])(?!\d)", cleaned)
        if nums and len(txt)<=240:
            return int(nums[0])
    return ""


def extract_ai_marks(page):
    return (
        extract_ai_number(page,"本命"),
        extract_ai_number(page,"対抗"),
        extract_ai_number(page,"単穴"),
        extract_ai_number(page,"連下"),
    )


def mark_for_rider(r, hon, tai, ana, ren):
    if r==hon: return "◎"
    if r==tai: return "○"
    if r==ana: return "△"
    if r==ren: return "×"
    return "他"


def line_mark_order(groups, hon, tai, ana, ren):
    return ".".join("".join(mark_for_rider(r,hon,tai,ana,ren) for r in g) for g in groups)


def three_line_order(groups, hon, tai, ana, ren):
    for g in groups:
        if len(g)==3:
            return "".join(mark_for_rider(r,hon,tai,ana,ren) for r in g)
    return ""


def _collect_riders(page):
    try:
        vals=page.evaluate("""()=> {
          const out=[],seen=new Set();
          for(const sel of ['[class*="RaceCard"] [class*="Bib"]','[class*="Racer"] [class*="Bib"]','[class*="Bib"]']){
            for(const e of document.querySelectorAll(sel)){
              const cls=typeof e.className==='string'?e.className:'';
              if(cls.includes('LinePowerBib'))continue;
              const t=(e.innerText||'').trim();
              if(/^[1-9]$/.test(t)&&!seen.has(t)){seen.add(t);out.push(Number(t));}
            }
            if(out.length>=5)break;
          }
          return out;
        }""")
        return vals if 5 <= len(vals) <= 9 else []
    except Exception:
        return []


def extract_line(page, stop_event=None):
    """明示されたラインだけ読む。取れない時は推測しない。"""
    _check_stop(stop_event)
    all_riders=_collect_riders(page)
    formed=[]

    for attempt in range(2):
        _check_stop(stop_event)
        try:
            heading=page.get_by_text(re.compile("ラインパワー比較"))
            if heading.count():
                heading.first.scroll_into_view_if_needed(timeout=1800)
                page.wait_for_timeout(250 + 250*attempt)
                data=heading.first.evaluate("""e=>{
                    let root=e;
                    for(let k=0;k<10&&root;k++,root=root.parentElement){
                      const groups=[];
                      for(const li of root.querySelectorAll('li')){
                        const nums=[];
                        for(const b of li.querySelectorAll('[class*="LinePowerBib"]')){
                          const t=(b.innerText||'').trim();
                          if(/^[1-9]$/.test(t)&&!nums.includes(Number(t)))nums.push(Number(t));
                        }
                        if(nums.length>=2)groups.push(nums);
                      }
                      if(groups.length)return groups;
                    }
                    return [];
                }""")
                if data:
                    formed=data
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

    used=[]
    for g in formed:
        for n in g:
            if n not in used:
                used.append(n)

    groups=list(formed)
    if all_riders:
        groups += [[n] for n in all_riders if n not in used]

    counts=[len(g) for g in groups]
    if 4 <= sum(counts) <= 9 and len(groups)>=2:
        return ".".join(map(str,counts)), groups
    return "", []


def race_info(url):
    m=re.search(r"/raceresult/(\d{8})(\d*)/(\d+)/(\d+)", url)
    if not m:
        return None
    start=datetime.strptime(m.group(1),"%Y%m%d").date()
    sid=m.group(1)+m.group(2)
    day=int(m.group(3)); race=int(m.group(4))
    actual=start+timedelta(days=day-1)
    return sid,day,race,actual


def parse_start_time(body):
    m=re.search(r"発走(?:予定)?[\s：:]*([0-2]?\d:[0-5]\d)", body or "")
    return m.group(1) if m else ""


def discover_today_venues(p, today, progress, stop_event):
    """今日開催の場だけ拾う。"""
    _check_stop(stop_event)
    browser=context=page=None
    try:
        browser=p.chromium.launch(headless=True,args=_browser_args())
        context=browser.new_context(locale="ja-JP",viewport={"width":900,"height":700},service_workers="block")
        enable_fast_mode(context)
        page=context.new_page()
        page.set_default_timeout(6500)
        body=goto(page,f"{BASE}/keirin/racecard/{today.strftime('%Y%m%d')}",stop_event)
        slugs=[];seen=set()
        links=page.locator("a[href*='/keirin/']")
        for i in range(links.count()):
            href=links.nth(i).get_attribute("href") or ""
            m=re.search(r"/keirin/([^/]+)/(?:racecard|raceresult|predictions)/",href)
            if m and m.group(1) in VENUES.values() and m.group(1) not in seen:
                seen.add(m.group(1));slugs.append(m.group(1))
        if not slugs:
            for venue,slug in VENUES.items():
                if f"{venue}競輪" in body:
                    slugs.append(slug)
        rev={v:k for k,v in VENUES.items()}
        return [(rev[s],s) for s in slugs if s in rev]
    finally:
        for obj in (page,context,browser):
            try:
                if obj: obj.close()
            except Exception: pass


def venue_races_and_grade(p, venue, slug, today, stop_event):
    """
    場単位の最初の安い判定。
    月間結果ページから今日のレースURLを作り、
    1Rだけ開いて場の開催ランクを先に見る。
    F1なら場ごと終了。
    """
    browser=context=page=None
    try:
        _check_stop(stop_event)
        browser=p.chromium.launch(headless=True,args=_browser_args())
        context=browser.new_context(locale="ja-JP",viewport={"width":900,"height":700},service_workers="block")
        enable_fast_mode(context)
        page=context.new_page()
        page.set_default_timeout(6500)

        month_url=f"{BASE}/keirin/{slug}/raceresult/{today.year}{today.month:02d}"
        goto(page,month_url,stop_event)

        rows=[]
        seen=set()
        for i in range(page.locator("a[href*='/raceresult/']").count()):
            href=page.locator("a[href*='/raceresult/']").nth(i).get_attribute("href") or ""
            info=race_info(href)
            if not info: continue
            sid,day_no,race_no,actual=info
            if actual != today: continue
            full=href if href.startswith("http") else BASE+href
            key=(sid,day_no,race_no)
            if key not in seen:
                seen.add(key)
                rows.append({"result_url":full,"sid":sid,"day":day_no,"race":race_no})
        rows.sort(key=lambda x:x["race"])

        if not rows:
            return "", [], None

        # 場の開催ランクを先判定。
        # 1RがL級のことがあるので、最初の「F1/F2」が見つかるまでだけ確認する。
        # F1が見つかった時点で、この場は後続レースを全部パスできる。
        first_body=""
        leading_l=[]
        grade=""
        for probe in rows:
            _check_stop(stop_event)
            pred_url=f"{BASE}/keirin/{slug}/predictions/{probe['sid']}/{probe['day']}/{probe['race']}"
            body=goto(page,pred_url,stop_event)
            if not first_body:
                first_body=body
            g=parse_grade(body)
            if g=="L":
                leading_l.append(probe["race"])
                continue
            if g in ("F1","F2"):
                grade=g
                break

        return grade, rows, {"first_body":first_body,"leading_l":leading_l}
    finally:
        for obj in (page,context,browser):
            try:
                if obj: obj.close()
            except Exception: pass


def scan_f2_venue(p, venue, slug, today, rows, progress, stop_event, counters):
    """
    F2場のみ、レースごとに:
    L級なら即パス → 星2 → ライン → 印
    の順で安い条件から落とす。
    """
    matches=[]; errors=[]; skipped=[]
    browser=context=page=None
    try:
        browser=p.chromium.launch(headless=True,args=_browser_args())
        context=browser.new_context(locale="ja-JP",viewport={"width":900,"height":700},service_workers="block")
        enable_fast_mode(context)
        page=context.new_page()
        page.set_default_timeout(6500)

        for pos,row in enumerate(rows,1):
            _check_stop(stop_event)
            race=row["race"]
            progress({
                "phase":"races",
                "current":f"{venue} {race}R",
                "detail":"L級確認 → 星2 → ライン → 印",
            })

            pred_url=f"{BASE}/keirin/{slug}/predictions/{row['sid']}/{row['day']}/{race}"
            try:
                body=goto(page,pred_url,stop_event)
                counters["checked_races"] += 1

                grade=parse_grade(body)
                # F2場の途中にL級があればそのレースだけ即パス
                if grade=="L":
                    counters["girls_l"] += 1
                    skipped.append({"venue":venue,"race":race,"reason":"L級ガールズ"})
                    continue

                # F2場内でもページ上F2確認できたレースだけ先へ
                if grade and grade!="F2":
                    counters["non_f2_races"] += 1
                    skipped.append({"venue":venue,"race":race,"reason":grade})
                    continue

                counters["f2_races"] += 1

                star,_=confidence_dom(page)
                if star is None:
                    # 少しだけ遅延描画待ち
                    page.wait_for_timeout(300)
                    star,_=confidence_dom(page)
                if star != TARGET_STAR:
                    continue
                counters["star2"] += 1

                line,groups=extract_line(page,stop_event)
                if line not in TARGET_LINES:
                    continue
                counters["line_target"] += 1

                hon,tai,ana,ren=extract_ai_marks(page)
                if hon in ("",None) or tai in ("",None):
                    continue

                order3=three_line_order(groups,hon,tai,ana,ren)
                if order3 not in TARGET_ORDERS:
                    continue
                counters["order_target"] += 1

                start_time=""
                try:
                    result_body=goto(page,row["result_url"],stop_event)
                    start_time=parse_start_time(result_body)
                except Exception:
                    pass

                match={
                    "venue":venue,
                    "slug":slug,
                    "race":race,
                    "time":start_time,
                    "star":"星2",
                    "line":line,
                    "three_order":order3,
                    "order":line_mark_order(groups,hon,tai,ana,ren),
                    "prediction_url":pred_url,
                }
                matches.append(match)
                counters["matched"] += 1

            except StopRequested:
                raise
            except Exception as e:
                msg=f"{venue} {race}R: {type(e).__name__}: {e}"
                errors.append(msg)
                counters["errors"] += 1

        return matches, errors, skipped
    finally:
        for obj in (page,context,browser):
            try:
                if obj: obj.close()
            except Exception: pass


def scan_today(progress=None, stop_event=None):
    if progress is None:
        progress=lambda x:None

    counters={
        "venues_found":0,
        "f2_venues":0,
        "f1_venues":0,
        "other_venues":0,
        "checked_races":0,
        "girls_l":0,
        "non_f2_races":0,
        "f2_races":0,
        "star2":0,
        "line_target":0,
        "order_target":0,
        "matched":0,
        "errors":0,
    }

    result={
        "date":_today_jst().isoformat(),
        "matches":[],
        "errors":[],
        "skipped":[],
        "counters":counters,
        "stopped":False,
    }

    try:
        _check_stop(stop_event)
        if not ensure_browser(progress):
            raise RuntimeError("Chromiumが見つかりません。Renderの再デプロイをしてください。")

        today=_today_jst()
        progress({"phase":"venues","current":"今日の開催場を確認中","detail":""})

        with sync_playwright() as p:
            venues=discover_today_venues(p,today,progress,stop_event)
            counters["venues_found"]=len(venues)

            for vi,(venue,slug) in enumerate(venues,1):
                _check_stop(stop_event)
                progress({
                    "phase":"venue_grade",
                    "current":f"{venue} ({vi}/{len(venues)})",
                    "detail":"まず場のランクだけ確認",
                })

                try:
                    grade,rows,_=venue_races_and_grade(p,venue,slug,today,stop_event)
                except StopRequested:
                    raise
                except Exception as e:
                    result["errors"].append(f"{venue}: 場確認 {type(e).__name__}: {e}")
                    counters["errors"] += 1
                    continue

                if grade=="F1":
                    counters["f1_venues"] += 1
                    result["skipped"].append({"venue":venue,"reason":"F1場を丸ごとパス"})
                    continue

                if grade=="L":
                    # もし場の先頭がLなら、F2混在の可能性があるため場全体は落とさずレース単位へ。
                    # ただし「F2場」の判定には数えない。
                    counters["other_venues"] += 1

                elif grade=="F2":
                    counters["f2_venues"] += 1
                else:
                    counters["other_venues"] += 1
                    # 不明時は安全側でレース単位確認（取りこぼし防止）

                # F1以外のみレース単位へ。L級は各レースで即パス。
                m,e,s=scan_f2_venue(
                    p,venue,slug,today,rows,progress,stop_event,counters
                )
                result["matches"].extend(m)
                result["errors"].extend(e)
                result["skipped"].extend(s)

        result["matches"].sort(key=lambda x:(x.get("time") or "99:99",x["venue"],x["race"]))
        progress({"phase":"done","current":"検索完了","detail":""})
        return result

    except StopRequested:
        result["stopped"]=True
        progress({"phase":"stopped","current":"途中停止","detail":"停止しました"})
        return result
