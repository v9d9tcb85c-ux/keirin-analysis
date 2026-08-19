# -*- coding: utf-8 -*-
import re
import time
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

BASE = "https://www.winticket.jp"
MAX_RETRIES = 2

def _log(msg):
    try:
        print(f"[WINTICKET] {msg}", flush=True)
    except Exception:
        pass


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

def race_info(url):
    m = re.search(r"/raceresult/(\d{8})(\d*)/(\d+)/(\d+)", url)
    if not m:
        return None
    start = datetime.strptime(m.group(1), "%Y%m%d").date()
    schedule_id = m.group(1) + m.group(2)
    day = int(m.group(3))
    race = int(m.group(4))
    actual = start + timedelta(days=day - 1)
    return schedule_id, day, race, actual
def enable_fast_mode(context):
    """
    高速化:
    - 画像 / 動画 / 音声 / フォントを読み込まない
    - 一部の計測・広告系URLを遮断
    DOM/本文取得に必要な document / script / xhr / fetch は残す
    """
    blocked_keywords = [
        "doubleclick", "googletagmanager", "google-analytics",
        "adservice", "adsystem", "facebook.net", "clarity.ms"
    ]

    def handler(route):
        req = route.request
        rtype = req.resource_type
        url = req.url.lower()

        if rtype in ("image", "media", "font"):
            route.abort()
            return

        if any(k in url for k in blocked_keywords):
            route.abort()
            return

        route.continue_()

    context.route("**/*", handler)
def goto(page, url):
    """
    v46.5高速版:
    domcontentloaded後の固定待ちを900ms→150msへ短縮。
    bodyが取れなければ短時間だけ再待機。
    """
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(150)
            try:
                return page.locator("body").inner_text(timeout=5000)
            except Exception:
                page.wait_for_timeout(350)
                return page.locator("body").inner_text(timeout=10000)
        except Exception as e:
            last = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(0.8)
    raise last
def parse_grade(body):
    m = re.search(r"(?<![A-Z0-9])(F1|F2)(?![A-Z0-9])", body)
    return m.group(1) if m else ""
def parse_session(text):
    if "ミッドナイト" in text: return "MN"
    if "モーニング" in text: return "M"
    if "ナイター" in text: return "N"
    return "D"
def parse_start_time_dom(page, body, race_no):
    """
    発走時刻をDOMから取得。
    1) time要素や時刻だけの小さな要素を集める
    2) 親要素に「発走」または対象Rがある候補を優先
    3) 本文の「発走 HH:MM」をフォールバック
    """
    try:
        candidates = page.evaluate("""(raceNo) => {
            const out=[];
            const re=/^(?:[01]?\\d|2[0-3]):[0-5]\\d$/;
            const nodes=[...document.querySelectorAll('time, span, div, p, td, th, li')];
            for (const e of nodes) {
                const t=(e.innerText||'').trim();
                if (!re.test(t)) continue;
                let score=0;
                let context='';
                let n=e;
                for(let k=0;k<5 && n;k++,n=n.parentElement){
                    const tx=(n.innerText||'').trim();
                    if(tx.length<=300) context += ' ' + tx;
                }
                if(context.includes('発走')) score += 10;
                if(context.includes(String(raceNo)+'R')) score += 7;
                if(context.includes('締切')) score -= 3;
                out.push({time:t,score,context:context.slice(0,500)});
            }
            out.sort((a,b)=>b.score-a.score);
            return out.slice(0,30);
        }""", race_no)
        if candidates:
            # Prefer a candidate positively tied to race header/発走.
            for c in candidates:
                if c.get("score",0) > 0:
                    return c.get("time","")
            # If only one plausible time exists, accept it.
            unique=[]
            for c in candidates:
                if c.get("time") not in unique:
                    unique.append(c.get("time"))
            if len(unique) == 1:
                return unique[0]
    except Exception:
        pass

    # Strong text fallbacks.
    patterns = [
        r"発走(?:予定)?[\\s：:]*([0-2]?\\d:[0-5]\\d)",
        rf"{race_no}R[\\s\\S]{{0,120}}?([0-2]?\\d:[0-5]\\d)",
    ]
    for p in patterns:
        m = re.search(p, body[:5000])
        if m:
            return m.group(1)

    # Last fallback: time element text.
    try:
        loc = page.locator("time")
        for i in range(min(loc.count(),10)):
            t=(loc.nth(i).inner_text(timeout=500) or "").strip()
            if re.fullmatch(r"(?:[01]?\\d|2[0-3]):[0-5]\\d", t):
                return t
    except Exception:
        pass
    return ""
def confidence_dom(page):
    loc = page.locator('[aria-label*="3点中"]')
    for i in range(loc.count()):
        try:
            label = loc.nth(i).get_attribute("aria-label") or ""
        except:
            continue
        m = re.search(r"3点中\s*([0-3])点", label)
        if m:
            return int(m.group(1)), label
    try:
        html = page.content()
        m = re.search(r'aria-label=["\']3点中\s*([0-3])点["\']', html)
        if m:
            return int(m.group(1)), f"3点中{m.group(1)}点"
    except:
        pass
    return None, ""
def ancestor_texts(page, label, levels=8):
    out = []
    try:
        loc = page.get_by_text(label, exact=True)
        if loc.count() == 0:
            loc = page.get_by_text(re.compile(re.escape(label)))
        for i in range(min(loc.count(), 6)):
            el = loc.nth(i)
            try:
                vals = el.evaluate(f"""e => {{
                    const out=[]; let n=e;
                    for(let k=0;k<{levels};k++){{
                        if(!n) break;
                        out.push((n.innerText||'').trim());
                        n=n.parentElement;
                    }}
                    return out;
                }}""")
                for v in vals:
                    if v and v not in out:
                        out.append(v)
            except:
                pass
    except:
        pass
    return out
def extract_ai_number(page, label):
    for txt in ancestor_texts(page, label, 7):
        cleaned = txt.replace(label, " ")
        nums = re.findall(r"(?<!\d)([1-9])(?!\d)", cleaned)
        if len(nums) == 1 and len(txt) <= 120:
            return int(nums[0]), txt
    for txt in ancestor_texts(page, label, 7):
        cleaned = txt.replace(label, " ")
        nums = re.findall(r"(?<!\d)([1-9])(?!\d)", cleaned)
        if nums and len(txt) <= 220:
            return int(nums[0]), txt
    return "", ""
def extract_ai_marks_dom(page):
    hon, rh = extract_ai_number(page, "本命")
    tai, ro = extract_ai_number(page, "対抗")
    ana, rt = extract_ai_number(page, "単穴")
    ren, rx = extract_ai_number(page, "連下")
    return hon, tai, ana, ren, {"本命":rh,"対抗":ro,"単穴":rt,"連下":rx}
def extract_line_shape_li(page):
    """
    v46.5 safe lineup extraction.

    Rules:
      1) Explicit formed lines must be detected from ラインパワー比較.
      2) All visible starter bibs are collected independently.
      3) Only when at least one explicit formed line exists, riders absent from
         those formed lines may be appended as singleton riders.
      4) If no formed line is detected, NEVER fabricate 1.1.1... . Return blank
         and keep diagnostics instead.

    This supports 5/6/7-rider races without assuming a fixed field size.
    """
    diag={
        "strategy":"explicit-lines-plus-confirmed-singletons",
        "section_found":False,
        "lis":[],
        "all_riders":[],
        "missing_singletons":[],
        "status":""
    }

    # Collect starter bibs from multiple likely rider-list regions.
    # Do not use LinePowerBib here; this list should represent the full field.
    try:
        all_riders=page.evaluate("""() => {
            const out=[];
            const seen=new Set();
            const selectors=[
              '[class*="RaceCard"] [class*="Bib"]',
              '[class*="Racer"] [class*="Bib"]',
              '[class*="Prediction"] [class*="Bib"]',
              '[class*="Bib___Wrapper"]',
              '[class*="Bib"]'
            ];
            for(const sel of selectors){
              let nodes=[];
              try{ nodes=[...document.querySelectorAll(sel)]; }catch(e){}
              for(const e of nodes){
                const cls=(typeof e.className==='string'?e.className:'');
                if(cls.includes('LinePowerBib')) continue;
                const t=(e.innerText||'').trim();
                if(/^[1-9]$/.test(t) && !seen.has(t)){
                  seen.add(t); out.push(Number(t));
                }
              }
              if(out.length>=5) break;
            }
            return out;
        }""")
        if 5 <= len(all_riders) <= 9:
            diag["all_riders"]=all_riders
        else:
            all_riders=[]
            diag["all_riders_status"]="unreliable-count"
    except Exception as e:
        all_riders=[]
        diag["all_riders_error"]=str(e)

    heading=page.get_by_text(re.compile("ラインパワー比較"))
    formed_groups=[]

    if heading.count()>0:
        for hi in range(min(heading.count(),5)):
            h=heading.nth(hi)
            try:
                result=h.evaluate("""e => {
                    function riderData(li){
                        const bibs=[...li.querySelectorAll('[class*="LinePowerBib"]')];
                        if(bibs.length){
                            return {method:'LinePowerBib',texts:bibs.map(x=>(x.innerText||'').trim())};
                        }
                        return {method:'none',texts:[]};
                    }
                    let root=e;
                    for(let k=0;k<8 && root;k++,root=root.parentElement){
                        const lis=[...root.querySelectorAll(':scope li')];
                        if(lis.length>=1){
                            const info=lis.map(li=>{
                                const rd=riderData(li);
                                return {
                                    text:(li.innerText||'').trim(),
                                    cls:(typeof li.className==='string'?li.className:''),
                                    method:rd.method,
                                    riderTexts:rd.texts,
                                    html:li.outerHTML.slice(0,2500)
                                };
                            });
                            const useful=info.filter(x=>x.riderTexts && x.riderTexts.length);
                            if(useful.length){
                                return {
                                  sectionFound:true,
                                  rootText:(root.innerText||'').trim().slice(0,3000),
                                  rootHtml:root.outerHTML.slice(0,5000),
                                  lis:useful
                                };
                            }
                        }
                    }
                    return {sectionFound:false,lis:[]};
                }""")
                if not result or not result.get('sectionFound'):
                    continue
                diag['section_found']=True
                diag['root_text']=result.get('rootText','')
                diag['root_html']=result.get('rootHtml','')
                diag['lis']=result.get('lis',[])

                groups=[]
                for li in diag['lis']:
                    nums=[]
                    for t in li.get('riderTexts',[]):
                        m=re.fullmatch(r'\s*([1-9])\s*',str(t))
                        if m:
                            n=int(m.group(1))
                            if n not in nums:
                                nums.append(n)
                    # A formed line must contain at least 2 riders.
                    if len(nums) >= 2:
                        groups.append(nums)
                if groups:
                    formed_groups=groups
                    break
            except Exception as e:
                diag['error']=str(e)

    # Critical safety rule: do not turn a completely missing lineup into all singletons.
    if not formed_groups:
        diag['status']='no-explicit-lines; left-blank'
        return '', [], diag

    used=[]
    for g in formed_groups:
        for n in g:
            if n not in used:
                used.append(n)

    groups=list(formed_groups)

    # Add confirmed singleton riders only from a reliable full-field bib list.
    if all_riders:
        missing=[n for n in all_riders if n not in used]
        diag['missing_singletons']=missing
        groups += [[n] for n in missing]
        diag['status']='explicit-lines + confirmed-singletons'
    else:
        diag['status']='explicit-lines only; full-field list unavailable'

    counts=[len(g) for g in groups]
    total=sum(counts)
    if groups and 2 <= len(groups) and 4 <= total <= 9:
        return '.'.join(map(str,counts)), groups, diag

    diag['status']='invalid-group-total; left-blank'
    return '', [], diag
def mark_for_rider(rider, hon, tai, ana, ren):
    if rider == hon: return "◎"
    if rider == tai: return "○"
    if rider == ana: return "△"
    if rider == ren: return "×"
    return "他"
def line_mark_order(groups, hon, tai, ana, ren):
    return ".".join(
        "".join(mark_for_rider(r, hon, tai, ana, ren) for r in group)
        for group in groups
    )

TARGET_LINES = {"3.2.2", "2.3.2", "2.2.3"}
TARGET_ORDERS = {"◎○△", "◎○×"}

def three_line_order(groups, hon, tai, ana, ren):
    for group in groups:
        if len(group) == 3:
            return "".join(mark_for_rider(r, hon, tai, ana, ren) for r in group)
    return ""

def _looks_unpublished(text):
    """AI予想/ラインがまだ公開前と思われる状態を判定。"""
    if not text:
        return True
    markers = [
        "予想情報はありません",
        "予想はまだありません",
        "AI予想はありません",
        "AI予想がありません",
        "予想公開前",
        "予想未公開",
        "ライン情報はありません",
        "ライン情報がありません",
        "ライン未公開",
        "公開前",
    ]
    return any(m in text for m in markers)


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

def _process_one_venue(browser, venue, slug, today, progress=None, venue_index=0, venue_total=0):
    matches = []
    unpublished = []
    errors = []
    checked = 0

    context = page = None

    try:
        context = browser.new_context(
            locale="ja-JP",
            viewport={"width": 900, "height": 700},
            service_workers="block",
        )
        enable_fast_mode(context)
        page = context.new_page()
        page.set_default_timeout(6500)

        _log(f"START venue={venue}")
        if progress:
            progress({
                "phase": "venues",
                "current": venue,
                "done": venue_index,
                "total": venue_total
            })

        month_url = f"{BASE}/keirin/{slug}/raceresult/{today.year}{today.month:02d}"
        try:
            goto(page, month_url)
        except Exception as e:
            msg = f"{venue}: 開催確認失敗: {type(e).__name__}: {e}"
            _log(msg)
            _log(f"URL {month_url}")
            errors.append(msg)
            return matches, unpublished, errors, checked

        candidates = []
        seen = set()

        try:
            links = page.locator("a[href*='/raceresult/']")
            for i in range(links.count()):
                href = links.nth(i).get_attribute("href") or ""
                info = race_info(href)
                if not info:
                    continue
                sid, day_no, race_no, actual = info
                if actual != today:
                    continue
                full = href if href.startswith("http") else BASE + href
                key = (race_no, full)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((full, sid, day_no, race_no))
        except Exception as e:
            msg = f"{venue}: レース一覧取得失敗: {type(e).__name__}: {e}"
            _log(msg)
            _log(f"URL {month_url}")
            errors.append(msg)
            return matches, unpublished, errors, checked

        venue_total_races = len(candidates)

        for ri, (result_url, sid, day_no, race_no) in enumerate(candidates, 1):
            checked += 1

            if progress:
                progress({
                    "phase": "races",
                    "current": f"{venue} {race_no}R",
                    "done": ri - 1,
                    "total": venue_total_races
                })

            pred_url = f"{BASE}/keirin/{slug}/predictions/{sid}/{day_no}/{race_no}"

            try:
                pred_body = goto(page, pred_url)
            except Exception as e:
                _log(f"PRED_FAIL venue={venue} race={race_no}R error={type(e).__name__}: {e}")
                _log(f"URL {pred_url}")
                unpublished.append({
                    "venue": venue,
                    "race": race_no,
                    "reason": "AI予想ページ未公開",
                    "prediction_url": pred_url
                })
                continue

            try:
                grade = parse_grade(pred_body)

                if not grade:
                    unpublished.append({
                        "venue": venue,
                        "race": race_no,
                        "reason": "レース情報未公開",
                        "prediction_url": pred_url
                    })
                    continue

                if grade != "F2":
                    continue

                conf, _ = confidence_dom(page)
                hon, tai, ana, ren, _ = extract_ai_marks_dom(page)

                if hon in ("", None) or tai in ("", None):
                    unpublished.append({
                        "venue": venue,
                        "race": race_no,
                        "reason": "AI予想未公開",
                        "prediction_url": pred_url
                    })
                    continue

                line, groups, _ = extract_line_shape_li(page)

                if not line or not groups:
                    unpublished.append({
                        "venue": venue,
                        "race": race_no,
                        "reason": "ライン未公開",
                        "prediction_url": pred_url
                    })
                    continue

                if line not in TARGET_LINES:
                    continue

                order3 = three_line_order(groups, hon, tai, ana, ren)

                if not order3:
                    unpublished.append({
                        "venue": venue,
                        "race": race_no,
                        "reason": "並び判定待ち",
                        "prediction_url": pred_url
                    })
                    continue

                if order3 not in TARGET_ORDERS:
                    continue

                full_order = line_mark_order(groups, hon, tai, ana, ren)

                start_time = ""
                session = ""

                try:
                    result_body = goto(page, result_url)
                    start_time = parse_start_time_dom(page, result_body, race_no) or ""
                    session = parse_session(result_body + "\n" + pred_body) or ""
                except Exception as e:
                    _log(f"RESULT_FAIL venue={venue} race={race_no}R error={type(e).__name__}: {e}")
                    _log(f"URL {result_url}")

                matches.append({
                    "venue": venue,
                    "race": race_no,
                    "time": start_time,
                    "day": session,
                    "star": f"星{conf}" if conf in (0, 1, 2, 3) else "判定不能",
                    "line": line,
                    "three_order": order3,
                    "order": full_order,
                    "prediction_url": pred_url
                })

            except Exception as e:
                if _looks_unpublished(pred_body):
                    unpublished.append({
                        "venue": venue,
                        "race": race_no,
                        "reason": "予想・ライン未公開",
                        "prediction_url": pred_url
                    })
                else:
                    msg = f"{venue} {race_no}R: {type(e).__name__}: {e}"
                    _log(f"RACE_ERROR {msg}")
                    _log(f"URL {pred_url}")
                    errors.append(msg)

    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass
    _log(f"END venue={venue} checked={checked} matches={len(matches)} unpublished={len(unpublished)} errors={len(errors)}")
    return matches, unpublished, errors, checked


def scan_today(progress=None):
    import gc
    import time as _time

    # Render is UTC, so use Japan time explicitly.
    JST = timezone(timedelta(hours=9))
    today = datetime.now(JST).date()

    all_matches = []
    all_unpublished = []
    all_errors = []
    checked_total = 0

    venue_items = list(VENUES.items())
    venue_total = len(venue_items)

    with sync_playwright() as p:
        browser = None
        try:
            for vi, (venue, slug) in enumerate(venue_items, 1):
                # Keep memory bounded on Render Free: reuse Chromium and recycle
                # it periodically instead of launching a new browser per venue.
                if browser is None or (vi > 1 and (vi - 1) % 8 == 0):
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass
                        gc.collect()
                    browser = p.chromium.launch(headless=True, args=_browser_args())

                if progress:
                    progress({
                        "phase": "venues",
                        "current": f"{venue} ({vi}/{venue_total})",
                        "done": vi - 1,
                        "total": venue_total
                    })

                try:
                    matches, unpublished, errors, checked = _process_one_venue(
                        browser, venue, slug, today,
                        progress=progress,
                        venue_index=vi - 1,
                        venue_total=venue_total
                    )
                    all_matches.extend(matches)
                    all_unpublished.extend(unpublished)
                    all_errors.extend(errors)
                    checked_total += checked
                except Exception as e:
                    msg = f"{venue}: {type(e).__name__}: {e}"
                    _log(f"VENUE_CRASH {msg}")
                    all_errors.append(msg)
                    try:
                        if browser is not None and not browser.is_connected():
                            browser = None
                    except Exception:
                        browser = None

                gc.collect()
                _time.sleep(0.15)
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    def time_key(x):
        return (x.get("time") or "99:99", x.get("venue", ""), x.get("race", 0))

    all_matches.sort(key=time_key)
    all_unpublished.sort(key=lambda x: (x.get("venue", ""), x.get("race", 0)))

    if progress:
        progress({
            "phase": "done",
            "current": "完了",
            "done": venue_total,
            "total": venue_total
        })

    _log(f"SCAN_DONE checked={checked_total} matches={len(all_matches)} unpublished={len(all_unpublished)} errors={len(all_errors)}")
    return {
        "date": today.isoformat(),
        "matches": all_matches,
        "checked_races": checked_total,
        "unpublished": all_unpublished,
        "errors": all_errors
    }
