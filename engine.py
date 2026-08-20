# -*- coding: utf-8 -*-
import re
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "https://www.winticket.jp"
ENGINE_VERSION="select-engine-v1.0"
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


class StopRequested(Exception):
    pass


def _log(msg):
    print(f"[WINTICKET] {msg}", flush=True)


def _log_race(venue, race, event, **kwargs):
    extra=" ".join(f"{k}={v}" for k,v in kwargs.items() if v not in (None,""))
    suffix=(" "+extra) if extra else ""
    _log(f"RACE venue={venue} race={race}R event={event}{suffix}")


def _log_venue(venue, event, **kwargs):
    extra=" ".join(f"{k}={v}" for k,v in kwargs.items() if v not in (None,""))
    suffix=(" "+extra) if extra else ""
    _log(f"VENUE venue={venue} event={event}{suffix}")


def _today_jst():
    return datetime.now(JST).date()


def _check_stop(stop_event):
    if stop_event and stop_event.is_set():
        raise StopRequested()


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


def _body_text(page):
    """body.inner_text timeout依存を避ける。"""
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        try:
            return page.locator("body").text_content(timeout=2500) or ""
        except Exception:
            return ""


def goto(page, url, stop_event=None):
    """
    v2.5.5:
    - 同じURLの自動再試行をしない（重複アクセス防止）
    - 最大6秒。timeoutでも本文が取得済みなら成功扱い
    - 前後で停止要求を確認
    """
    _check_stop(stop_event)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=6000)
        _check_stop(stop_event)
        page.wait_for_timeout(100)
        _check_stop(stop_event)
        body = _body_text(page)
        if body:
            return body
        raise RuntimeError("ページ本文を取得できません")
    except StopRequested:
        raise
    except Exception as e:
        _check_stop(stop_event)
        try:
            body = _body_text(page)
        except Exception:
            body = ""
        if body:
            _log(
                f"GOTO_PARTIAL_OK attempt=1/1 timeout_ms=6000 "
                f"url={url} error={type(e).__name__}:{e}"
            )
            return body
        _log(
            f"GOTO_FAIL attempt=1/1 timeout_ms=6000 "
            f"url={url} error={type(e).__name__}:{e}"
        )
        raise

def parse_grade(text):
    """
    小さい局所テキスト専用。
    L1/L級/ガールズをF1/F2より優先する。
    ページ全体の本文には使わない。
    """
    if not text:
        return ""

    if re.search(r"(?<![A-Z0-9])L1(?![A-Z0-9])", text):
        return "L"
    if "L級" in text or "ガールズ" in text or "女子" in text:
        return "L"

    m = re.search(r"(?<![A-Z0-9])(F1|F2)(?![A-Z0-9])", text)
    return m.group(1) if m else ""


def detect_grade_from_header(page, race_no):
    """
    予想ページ全体ではなく、画面上部のレース見出し周辺だけで級を判定。
    未発表ページ内の別レース/ナビゲーションにあるL1を誤取得しない。
    """
    try:
        texts = page.evaluate("""(raceNo)=>{
          const out=[];
          const nodes=[...document.querySelectorAll('h1,h2,h3,[class*="Header"],[class*="Title"],[class*="RaceInfo"],[class*="RaceHeader"]')];
          for(const e of nodes){
            const t=(e.innerText||'').trim();
            if(!t || t.length>260) continue;
            if(t.includes(String(raceNo)+'R') || /(?:F1|F2|L1|L級|ガールズ)/.test(t)){
              out.push(t);
            }
            if(out.length>=20) break;
          }
          return out;
        }""", race_no)
        for t in texts or []:
            g=parse_grade(t)
            if g:
                return g
    except Exception:
        pass
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
    _check_stop(stop_event)
    all_riders=_collect_riders(page)
    formed=[]

    for attempt in range(2):
        _check_stop(stop_event)
        try:
            heading=page.get_by_text(re.compile("ラインパワー比較"))
            if heading.count():
                heading.first.scroll_into_view_if_needed(timeout=1600)
                page.wait_for_timeout(220 + 220*attempt)
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
            page.evaluate("window.scrollBy(0,650)")
            page.wait_for_timeout(220)
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


def discover_today_venues(page, today, stop_event):
    _check_stop(stop_event)
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
            if f"{venue}競輪" in body and slug not in seen:
                seen.add(slug);slugs.append(slug)
    rev={v:k for k,v in VENUES.items()}
    return [(rev[s],s) for s in slugs if s in rev]


def get_today_races(page, slug, today, stop_event):
    _check_stop(stop_event)
    month_url=f"{BASE}/keirin/{slug}/raceresult/{today.year}{today.month:02d}"
    goto(page,month_url,stop_event)

    rows=[]
    seen=set()
    links=page.locator("a[href*='/raceresult/']")
    for i in range(links.count()):
        link=links.nth(i)
        href=link.get_attribute("href") or ""
        info=race_info(href)
        if not info:
            continue
        sid,day_no,race_no,actual=info
        if actual != today:
            continue
        key=(sid,day_no,race_no)
        if key in seen:
            continue
        seen.add(key)

        local_text=""
        try:
            local_text=link.evaluate("""e=>{
              let n=e;
              for(let k=0;k<6 && n;k++,n=n.parentElement){
                const t=(n.innerText||'').trim();
                if(t && t.length<=320 && (t.includes('R') || /F1|F2|L1|L級|ガールズ/.test(t))){
                  return t;
                }
              }
              return (e.innerText||'').trim();
            }""") or ""
        except Exception:
            pass

        rows.append({
            "sid":sid,
            "day":day_no,
            "race":race_no,
            "grade_hint":parse_grade(local_text),
            "grade_source":local_text[:240],
        })

    rows.sort(key=lambda x:x["race"])
    return rows


def detect_session(rows):
    """開催区分: M=モーニング / D=デイ / N=ナイター / MN=ミッドナイト"""
    text=" ".join((r.get("grade_source") or "") for r in rows)
    if "ミッドナイト" in text:
        return "MN"
    if "ナイター" in text:
        return "N"
    if "モーニング" in text:
        return "M"

    mins=[]
    for r in rows:
        for h,m in re.findall(r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)", r.get("grade_source") or ""):
            hh=int(h); mm=int(m)
            if 0 <= hh <= 23:
                mins.append(hh*60+mm)
    if mins:
        last=max(mins)
        if last >= 21*60:
            return "MN"
        if last >= 18*60:
            return "N"
        if last <= 14*60:
            return "M"
        return "D"
    return "?"

def _progress(progress, counters, **kw):
    payload=dict(kw)
    payload["counters"]=dict(counters)
    progress(payload)


def scan_today(progress=None, stop_event=None):
    if progress is None:
        progress=lambda x:None

    today=_today_jst()
    counters={
        "venues_found":0,"f2_venues":0,"f1_venues":0,
        "checked_races":0,"girls_l":0,"unpublished":0,"f2_races":0,
        "star2":0,"line_target":0,"order_target":0,"matched":0,
        "errors":0,"duplicate_skips":0,
    }
    result={
        "date":today.isoformat(),"matches":[],"errors":[],"skipped":[],
        "counters":counters,"stopped":False,"venues_info":[],
    }

    browser=context=page=None
    visited_prediction_urls=set()

    def emit(**kw):
        kw.setdefault("matches", list(result["matches"]))
        kw.setdefault("venues_info", list(result["venues_info"]))
        _progress(progress,counters,**kw)

    def open_once(url, venue, race):
        _check_stop(stop_event)
        if url in visited_prediction_urls:
            counters["duplicate_skips"]+=1
            _log_race(venue,race,"DUPLICATE_REUSE",url=url)
            return False
        goto(page,url,stop_event)
        visited_prediction_urls.add(url)
        counters["checked_races"]+=1
        return True

    try:
        _check_stop(stop_event)
        _log(f"SCAN_START version={ENGINE_VERSION} date={today.isoformat()}")

        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=_browser_args())
            context=browser.new_context(
                locale="ja-JP",viewport={"width":900,"height":700},
                service_workers="block"
            )
            enable_fast_mode(context)
            page=context.new_page()
            page.set_default_timeout(4500)

            emit(phase="venues",current="今日の開催場を取得中",
                 detail="まず今日の開催場をすべて画面に出します。")
            venues=discover_today_venues(page,today,stop_event)
            counters["venues_found"]=len(venues)
            _log(f"VENUES_FOUND count={len(venues)} names={','.join(v for v,_ in venues)}")

            # -------- 1st pass: 全開催場の F1/F2 + M/D/N/MN を確定 --------
            venue_jobs=[]
            for vi,(venue,slug) in enumerate(venues,1):
                _check_stop(stop_event)
                emit(
                    phase="venue_board",
                    current=f"{venue} ({vi}/{len(venues)})",
                    detail=f"全{len(venues)}場中 {vi}場目：F1/F2・開催区分を確認",
                    venue_index=vi,venue_total=len(venues),
                )
                try:
                    rows=get_today_races(page,slug,today,stop_event)
                except StopRequested:
                    raise
                except Exception as e:
                    counters["errors"]+=1
                    result["errors"].append(f"{venue}: 一覧取得 {type(e).__name__}: {e}")
                    result["venues_info"].append({
                        "venue":venue,"slug":slug,"grade":"?","session":"?","status":"ERROR"
                    })
                    emit(phase="venue_board",current=venue,detail="開催情報の取得エラー",
                         venue_index=vi,venue_total=len(venues))
                    continue

                session=detect_session(rows)
                grade=""
                probe_index=None

                for idx,row in enumerate(rows):
                    _check_stop(stop_event)
                    race=row["race"]
                    hint=row.get("grade_hint") or ""
                    if hint=="L":
                        continue
                    pred_url=f"{BASE}/keirin/{slug}/predictions/{row['sid']}/{row['day']}/{race}"
                    try:
                        open_once(pred_url,venue,race)
                        grade=hint or detect_grade_from_header(page,race)
                        _log_race(venue,race,"VENUE_GRADE_RESULT",grade=grade or "UNKNOWN")
                        if grade=="L":
                            continue
                        if grade in ("F1","F2"):
                            probe_index=idx
                            break
                    except StopRequested:
                        raise
                    except Exception as e:
                        counters["errors"]+=1
                        _log_race(venue,race,"GRADE_ERROR",error=f"{type(e).__name__}:{e}")
                        continue

                if grade=="F1":
                    counters["f1_venues"]+=1
                    status="SKIP"
                elif grade=="F2":
                    counters["f2_venues"]+=1
                    status="TARGET"
                else:
                    status="UNKNOWN"

                info={"venue":venue,"slug":slug,"grade":grade or "?",
                      "session":session,"status":status}
                result["venues_info"].append(info)
                venue_jobs.append((venue,slug,rows,grade,probe_index,session))
                _log_venue(venue,"BOARD_READY",grade=grade or "UNKNOWN",session=session)
                emit(
                    phase="venue_board",current=venue,
                    detail=f"{venue}  {grade or '?'}  {session}",
                    venue_index=vi,venue_total=len(venues),
                )

            # -------- 2nd pass: F2だけ条件検索 --------
            f2_jobs=[j for j in venue_jobs if j[3]=="F2"]
            for fi,(venue,slug,rows,grade,probe_index,session) in enumerate(f2_jobs,1):
                _check_stop(stop_event)
                _log_venue(venue,"F2_SCAN_START",index=f"{fi}/{len(f2_jobs)}",session=session)

                for idx,row in enumerate(rows):
                    _check_stop(stop_event)
                    race=row["race"]
                    emit(
                        phase="races",
                        current=f"{venue} {race}R",
                        detail=f"F2検索 {fi}/{len(f2_jobs)}場：L級 → AI公開 → 星2 → ライン → 印",
                        venue_index=fi,venue_total=len(f2_jobs),
                    )

                    hint=row.get("grade_hint") or ""
                    if hint=="L":
                        counters["girls_l"]+=1
                        result["skipped"].append({"venue":venue,"race":race,"reason":"L級ガールズ"})
                        _log_race(venue,race,"SKIP_L_GIRLS",source="grade_hint")
                        continue

                    pred_url=f"{BASE}/keirin/{slug}/predictions/{row['sid']}/{row['day']}/{race}"
                    try:
                        # 場判定に使ったレースは、そのページが現在開いている保証がない。
                        # 重複アクセスはしない方針なので、既訪問なら再アクセスせず、
                        # そのレースだけは場判定時に条件検索できるよう future版でキャッシュ化する。
                        # v1.0ではF2場の条件検索を優先し、probeだけ再利用できない場合は1回だけ再取得。
                        if pred_url in visited_prediction_urls:
                            # grade probeアクセスは「重複検索」と数えず、条件取得のため1回だけ再表示。
                            goto(page,pred_url,stop_event)
                            _log_race(venue,race,"PROBE_REOPEN_FOR_CONDITION")
                        else:
                            open_once(pred_url,venue,race)

                        _check_stop(stop_event)
                        race_grade=hint or detect_grade_from_header(page,race)
                        if race_grade=="L":
                            counters["girls_l"]+=1
                            result["skipped"].append({"venue":venue,"race":race,"reason":"L級ガールズ"})
                            _log_race(venue,race,"SKIP_L_GIRLS",source="header")
                            continue

                        counters["f2_races"]+=1
                        star,_=confidence_dom(page)
                        if star is None:
                            page.wait_for_timeout(180)
                            _check_stop(stop_event)
                            star,_=confidence_dom(page)
                        _log_race(venue,race,"STAR_RESULT",
                                  star="UNPUBLISHED" if star is None else star)
                        if star is None:
                            counters["unpublished"]+=1
                            result["skipped"].append({"venue":venue,"race":race,"reason":"AI予想未発表"})
                            continue
                        if star != TARGET_STAR:
                            continue

                        counters["star2"]+=1
                        _check_stop(stop_event)
                        line,groups=extract_line(page,stop_event)
                        _log_race(venue,race,"LINE_RESULT",line=line or "NONE")
                        if line not in TARGET_LINES:
                            continue

                        counters["line_target"]+=1
                        _check_stop(stop_event)
                        hon,tai,ana,ren=extract_ai_marks(page)
                        if hon in ("",None) or tai in ("",None):
                            continue
                        order3=three_line_order(groups,hon,tai,ana,ren)
                        _log_race(venue,race,"ORDER3_RESULT",order3=order3 or "NONE")
                        if order3 not in TARGET_ORDERS:
                            continue

                        counters["order_target"]+=1
                        result["matches"].append({
                            "venue":venue,"slug":slug,"race":race,
                            "session":session,"star":"星2","line":line,
                            "three_order":order3,
                            "order":line_mark_order(groups,hon,tai,ana,ren),
                            "prediction_url":pred_url,
                        })
                        counters["matched"]+=1
                        _log_race(venue,race,"MATCH",line=line,order3=order3)
                        emit(
                            phase="races",current=f"{venue} {race}R",
                            detail="条件一致 → 買い目該当に追加",
                            venue_index=fi,venue_total=len(f2_jobs),
                        )
                    except StopRequested:
                        raise
                    except Exception as e:
                        counters["errors"]+=1
                        _log_race(venue,race,"ERROR",error=f"{type(e).__name__}:{e}")
                        result["errors"].append(f"{venue} {race}R: {type(e).__name__}: {e}")
                        continue

            result["matches"].sort(key=lambda x:(x["venue"],x["race"]))
            _log(
                "SCAN_DONE "
                f"venues={counters['venues_found']} f2venues={counters['f2_venues']} "
                f"f1venues={counters['f1_venues']} checked={counters['checked_races']} "
                f"girls={counters['girls_l']} unpublished={counters['unpublished']} "
                f"star2={counters['star2']} lines={counters['line_target']} "
                f"orders={counters['order_target']} matches={counters['matched']} "
                f"errors={counters['errors']}"
            )
            emit(phase="done",current="検索完了",detail="全F2場の検索が完了しました。")
            return result

    except StopRequested:
        result["stopped"]=True
        _log(f"SCAN_STOPPED checked={counters['checked_races']} matches={counters['matched']}")
        emit(phase="stopped",current="途中停止",detail="停止しました")
        return result
    finally:
        for obj in (page,context,browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
