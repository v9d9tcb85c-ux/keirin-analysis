# -*- coding: utf-8 -*-
import re
import time
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

BASE = "https://www.winticket.jp"
ENGINE_VERSION = "2.2-star2-diagnostic"
MAX_RETRIES = 3

JST = __import__("datetime").timezone(timedelta(hours=9))

def _today_jst():
    return datetime.now(JST).date()


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
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
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
    Render / PC共通の安全ライン取得版。

    PC版 v45.5 の「ラインパワー比較」基準を維持しつつ、
    Render(headless)で遅延描画される場合に備えて
    1) 見出しまでスクロール
    2) LinePowerBibの出現を短時間待機
    3) 最大3回だけ再取得
    を行う。

    ラインを確認できない場合は推測で作らず空欄を返す。
    """
    diag = {
        "strategy": "pc-v45.5 + render-lazy-retry",
        "section_found": False,
        "lis": [],
        "all_riders": [],
        "missing_singletons": [],
        "status": ""
    }

    def collect_all_riders():
        try:
            vals = page.evaluate("""() => {
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
            return vals if 5 <= len(vals) <= 9 else []
        except Exception:
            return []

    all_riders = collect_all_riders()
    diag["all_riders"] = all_riders

    formed_groups = []

    # Renderではライン比較部分が下方にあり遅延描画されることがあるため、
    # 見出しを表示領域へ移してから最大3回だけ確認する。
    for attempt in range(3):
        try:
            heading = page.get_by_text(re.compile("ラインパワー比較"))
            if heading.count() > 0:
                try:
                    heading.first.scroll_into_view_if_needed(timeout=1800)
                except Exception:
                    try:
                        heading.first.evaluate("e => e.scrollIntoView({block:'center'})")
                    except Exception:
                        pass

                # 初回250ms、再試行は少し長めに待つ。
                page.wait_for_timeout(250 + attempt * 300)

                for hi in range(min(heading.count(), 5)):
                    h = heading.nth(hi)
                    try:
                        result = h.evaluate("""e => {
                            function riderData(li){
                                const bibs=[...li.querySelectorAll('[class*="LinePowerBib"]')];
                                if(bibs.length){
                                    return {
                                      method:'LinePowerBib',
                                      texts:bibs.map(x=>(x.innerText||'').trim())
                                    };
                                }
                                return {method:'none',texts:[]};
                            }

                            let root=e;
                            for(let k=0;k<10 && root;k++,root=root.parentElement){
                                const lis=[...root.querySelectorAll('li')];
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

                        if not result or not result.get("sectionFound"):
                            continue

                        diag["section_found"] = True
                        diag["root_text"] = result.get("rootText", "")
                        diag["root_html"] = result.get("rootHtml", "")
                        diag["lis"] = result.get("lis", [])

                        groups = []
                        for li in diag["lis"]:
                            nums = []
                            for t in li.get("riderTexts", []):
                                m = re.fullmatch(r"\s*([1-9])\s*", str(t))
                                if m:
                                    n = int(m.group(1))
                                    if n not in nums:
                                        nums.append(n)
                            # 明示ラインは2車以上だけ採用
                            if len(nums) >= 2:
                                groups.append(nums)

                        if groups:
                            formed_groups = groups
                            diag["attempt"] = attempt + 1
                            break
                    except Exception as e:
                        diag["error"] = str(e)

            if formed_groups:
                break

            # 見出しがまだDOMに無いケース。ページ下方まで軽く動かして再描画を促す。
            try:
                page.evaluate("window.scrollBy(0, Math.max(500, window.innerHeight * 0.8))")
            except Exception:
                pass
            page.wait_for_timeout(250 + attempt * 250)

        except Exception as e:
            diag["retry_error"] = str(e)

    # class名変更対策:
    # LinePowerBib が0件でも「ラインパワー比較」配下の li に
    # 車番1桁だけの子要素が複数ある場合に限り、明示ラインとして拾う。
    # 2車以上のグループだけを採用し、推測で単騎を作ることはしない。
    if not formed_groups and diag.get("section_found"):
        try:
            fallback_groups = page.evaluate("""() => {
                const heads=[...document.querySelectorAll('body *')]
                  .filter(e=>(e.innerText||'').trim()==='ラインパワー比較');
                for(const h of heads){
                  let root=h;
                  for(let k=0;k<10 && root;k++,root=root.parentElement){
                    const groups=[];
                    for(const li of root.querySelectorAll('li')){
                      const nums=[];
                      for(const e of li.querySelectorAll('span,div,p,b,strong')){
                        const t=(e.innerText||'').trim();
                        if(/^[1-9]$/.test(t)){
                          const n=Number(t);
                          if(!nums.includes(n)) nums.push(n);
                        }
                      }
                      if(nums.length>=2 && nums.length<=4) groups.push(nums);
                    }
                    if(groups.length>=2) return groups;
                  }
                }
                return [];
            }""")
            # target候補として自然な構成だけ許可（合計6〜7車、重複車番なし）
            flat = [n for g in fallback_groups for n in g]
            if (fallback_groups and 6 <= len(flat) <= 7 and len(flat) == len(set(flat))):
                formed_groups = fallback_groups
                diag["fallback"] = "exact-digit-descendants"
                diag["status"] = "explicit-lines-by-fallback"
        except Exception as e:
            diag["fallback_error"] = str(e)

    # PC版と同じ安全策：ラインが取れないのに1.1.1...を捏造しない
    if not formed_groups:
        diag["status"] = "no-explicit-lines-after-retry; left-blank"
        return "", [], diag

    used = []
    for g in formed_groups:
        for n in g:
            if n not in used:
                used.append(n)

    groups = list(formed_groups)

    # PC版と同様、全出走車番が確実に取れた場合だけ単騎を補完
    if all_riders:
        missing = [n for n in all_riders if n not in used]
        diag["missing_singletons"] = missing
        groups += [[n] for n in missing]
        diag["status"] = "explicit-lines + confirmed-singletons"
    else:
        diag["status"] = "explicit-lines only; full-field list unavailable"

    counts = [len(g) for g in groups]
    total = sum(counts)

    if groups and 2 <= len(groups) and 4 <= total <= 9:
        return ".".join(map(str, counts)), groups, diag

    diag["status"] = "invalid-group-total; left-blank"
    return "", [], diag

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

TARGET_AI_STAR = 2
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


def _discover_today_venues(playwright, today, progress=None):
    """WINTICKETの日別出走表を1回だけ開き、今日実際に開催している場だけ抽出する。"""
    browser = context = page = None
    try:
        browser = playwright.chromium.launch(headless=True, args=_browser_args())
        context = browser.new_context(
            locale="ja-JP",
            viewport={"width": 900, "height": 700},
            service_workers="block",
        )
        enable_fast_mode(context)
        page = context.new_page()
        page.set_default_timeout(6500)

        url = f"{BASE}/keirin/racecard/{today.strftime('%Y%m%d')}"
        _log(f"DISCOVER url={url}")
        goto(page, url)

        active_slugs = []
        seen = set()
        links = page.locator("a[href*='/keirin/']")
        for i in range(links.count()):
            href = links.nth(i).get_attribute("href") or ""
            m = re.search(r"/keirin/([^/]+)/(?:racecard|raceresult|predictions)/", href)
            if not m:
                continue
            slug = m.group(1)
            if slug not in VENUES.values() or slug in seen:
                continue
            seen.add(slug)
            active_slugs.append(slug)

        # DOMのリンク構造が変わった場合は本文から開催場名を拾う。
        if not active_slugs:
            body = page.locator("body").inner_text(timeout=6500)
            for venue, slug in VENUES.items():
                if f"{venue}競輪" in body and slug not in seen:
                    seen.add(slug)
                    active_slugs.append(slug)

        reverse = {slug: venue for venue, slug in VENUES.items()}
        active = [(reverse[s], s) for s in active_slugs if s in reverse]
        _log("TODAY_VENUES " + ",".join(v for v, _ in active))
        return active
    except Exception as e:
        _log(f"DISCOVER_FAIL {type(e).__name__}: {e}")
        return []
    finally:
        for obj in (page, context, browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass



def _prediction_snapshot(page):
    """現在の予想ページから、判定材料がどこまでDOMに出ているかだけ確認する。"""
    try:
        return page.evaluate("""() => {
            const body=(document.body && document.body.innerText)||'';
            const star=[...document.querySelectorAll('[aria-label*="3点中"]')]
              .map(e=>e.getAttribute('aria-label')||'').filter(Boolean);
            const hasHon=body.includes('本命');
            const hasTai=body.includes('対抗');
            const hasLine=body.includes('ラインパワー比較');
            const bibs=[...document.querySelectorAll('[class*="LinePowerBib"]')]
              .map(e=>(e.innerText||'').trim()).filter(x=>/^[1-9]$/.test(x));
            return {
              bodyLen: body.length,
              star: star.slice(0,5),
              hasHon, hasTai, hasLine,
              lineBibCount: bibs.length
            };
        }""")
    except Exception:
        return {"bodyLen": 0, "star": [], "hasHon": False, "hasTai": False,
                "hasLine": False, "lineBibCount": 0}


def _settle_prediction_page(page, pred_url, venue, race_no):
    """
    Render/headlessでWINTICKETのReact描画が遅い場合の対策。
    固定で長く待つのではなく、AI星・本命/対抗・ラインのどれかが
    出るまで短い段階待機を行い、必要時のみ1回reloadする。
    """
    waits = (250, 450, 750)
    snap = _prediction_snapshot(page)

    def ready(s):
        # 最低限「AI予想領域」が描画され始めていれば次へ。
        return bool(s.get("star") or s.get("hasHon") or s.get("hasLine"))

    if ready(snap):
        return snap

    for ms in waits:
        try:
            page.wait_for_timeout(ms)
            # 遅延描画を起こすためページを段階的に下へ。
            page.evaluate("window.scrollBy(0, Math.max(500, window.innerHeight * 0.85))")
        except Exception:
            pass
        snap = _prediction_snapshot(page)
        if ready(snap):
            return snap

    # 情報公開済みなのに初回DOMだけ欠けるケースへの1回だけの再読込。
    try:
        _log(f"PRED_RELOAD venue={venue} race={race_no}R")
        page.reload(wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(650)
        snap = _prediction_snapshot(page)
    except Exception as e:
        _log(f"PRED_RELOAD_FAIL venue={venue} race={race_no}R error={type(e).__name__}: {e}")

    return snap


def _read_ai_star_retry(page):
    """PC版の星判定方式を維持したまま、遅延描画時だけ短く再試行。"""
    raw = ""
    for ms in (0, 250, 450):
        if ms:
            try: page.wait_for_timeout(ms)
            except Exception: pass
        star, raw = confidence_dom(page)
        if star is not None:
            return star, raw
    return None, raw


def _read_ai_marks_retry(page):
    """本命/対抗/単穴/連下が遅れて出る場合に短く再試行。"""
    last = ("", "", "", "", {})
    for ms in (0, 250, 450):
        if ms:
            try: page.wait_for_timeout(ms)
            except Exception: pass
        last = extract_ai_marks_dom(page)
        hon, tai, ana, ren, diag = last
        if hon not in ("", None) and tai not in ("", None):
            return last
    return last


def _race_diag(venue, race_no, **kw):
    """各レースがどこで落ちたかRenderログで一目で分かるようにする。"""
    parts = [f"{k}={v}" for k, v in kw.items()]
    _log(f"RACE_CHECK venue={venue} race={race_no}R " + " ".join(parts))



def _star2_trace(venue, race_no, step, **kw):
    """星2レースがどこまで進んだかを必ずRenderログへ残す。"""
    parts = [f"{k}={v}" for k, v in kw.items()]
    suffix = (" " + " ".join(parts)) if parts else ""
    _log(f"STAR2_TRACE venue={venue} race={race_no}R step={step}{suffix}")



def _star2_diag(venue, race_no, step, **kw):
    parts = [f"{k}={v}" for k, v in kw.items()]
    tail = (" " + " ".join(parts)) if parts else ""
    _log(f"STAR2_DIAG venue={venue} race={race_no}R step={step}{tail}")


def _process_one_venue(playwright, venue, slug, today, progress=None, venue_index=0, venue_total=0):
    matches = []
    unpublished = []
    errors = []
    checked = 0

    browser = context = page = None

    try:
        browser = playwright.chromium.launch(
            headless=True,
            args=_browser_args()
        )
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
                pred_snap = _settle_prediction_page(page, pred_url, venue, race_no)
                try:
                    pred_body = page.locator("body").inner_text(timeout=5000)
                except Exception:
                    pass
                _log(
                    f"PRED_READY venue={venue} race={race_no}R "
                    f"body={pred_snap.get('bodyLen',0)} "
                    f"starNodes={len(pred_snap.get('star',[]))} "
                    f"hon={pred_snap.get('hasHon',False)} "
                    f"line={pred_snap.get('hasLine',False)} "
                    f"lineBibs={pred_snap.get('lineBibCount',0)}"
                )
            except Exception as e:
                _log(f"PRED_FAIL venue={venue} race={race_no}R error={type(e).__name__}: {e}")
                _log(f"URL {pred_url}")
                unpublished.append({
                    "venue": venue,
                    "race": race_no,
                    "reason": "AI予想ページ未取得・再確認",
                    "prediction_url": pred_url
                })
                continue

            try:
                grade = parse_grade(pred_body)

                if not grade:
                    _race_diag(venue, race_no, result="WAIT_GRADE")
                    unpublished.append({
                        "venue": venue,
                        "race": race_no,
                        "reason": "レース情報未公開",
                        "prediction_url": pred_url
                    })
                    continue

                if grade != "F2":
                    _race_diag(venue, race_no, grade=grade, result="SKIP_NON_F2")
                    _log(f"SKIP_NON_F2 venue={venue} grade={grade}")
                    break

                # PC版 v45.5 と同じ方法で、WINTICKET AI予想の星表示を取得。
                # 見た目の ★★☆ はDOM内部では aria-label="3点中 2点" として取得できる。
                ai_star, ai_star_raw = _read_ai_star_retry(page)

                # 星表示がまだ取れない場合は「非該当」ではなく未公開/再判定待ち。
                if ai_star is None:
                    _race_diag(venue, race_no, grade=grade, star="None", result="WAIT_STAR")
                    unpublished.append({
                        "venue": venue,
                        "race": race_no,
                        "reason": "AI予想の星表示未公開・未取得",
                        "prediction_url": pred_url
                    })
                    _log(f"STAR_WAIT venue={venue} race={race_no}R raw={ai_star_raw}")
                    continue

                # 固定条件: F2の中でもAI予想が星2（★★☆）のレースだけ。
                if ai_star != TARGET_AI_STAR:
                    _race_diag(venue, race_no, grade=grade, star=ai_star, result="SKIP_STAR")
                    _log(f"SKIP_AI_STAR venue={venue} race={race_no}R star={ai_star}")
                    continue

                _log(f"AI_STAR2 venue={venue} race={race_no}R raw={ai_star_raw}")
                _star2_diag(venue, race_no, "STAR_OK", star=2)
                _star2_trace(venue, race_no, "STAR_OK", star=ai_star)
                conf = ai_star

                hon, tai, ana, ren, marks_diag = _read_ai_marks_retry(page)
                _star2_trace(
                    venue, race_no, "MARKS_READ",
                    hon=hon or "-", tai=tai or "-", ana=ana or "-", ren=ren or "-"
                )

                if hon in ("", None) or tai in ("", None):
                    _race_diag(
                        venue, race_no, grade=grade, star=ai_star,
                        hon=hon or "-", tai=tai or "-", result="WAIT_MARKS"
                    )
                    _star2_trace(venue, race_no, "STOP_MARKS", reason="AI予想印未取得")
                    unpublished.append({
                        "venue": venue,
                        "race": race_no,
                        "reason": "AI予想の印未取得・再確認",
                        "prediction_url": pred_url
                    })
                    continue

                _star2_trace(venue, race_no, "LINE_START")
                line, groups, line_diag = extract_line_shape_li(page)
                _star2_trace(
                    venue, race_no, "LINE_READ",
                    line=line or "-",
                    groups=groups or "-",
                    status=line_diag.get("status",""),
                    section=line_diag.get("section_found",False),
                    attempt=line_diag.get("attempt",0)
                )

                if not line or not groups:
                    _race_diag(
                        venue, race_no, grade=grade, star=ai_star,
                        hon=hon, tai=tai, line=line or "-",
                        line_status=line_diag.get("status",""),
                        result="WAIT_LINE"
                    )
                    _log(
                        f"LINE_WAIT venue={venue} race={race_no}R "
                        f"status={line_diag.get('status','')} "
                        f"section={line_diag.get('section_found',False)} "
                        f"attempt={line_diag.get('attempt',0)}"
                    )
                    _star2_trace(venue, race_no, "STOP_LINE", reason="ライン未取得")
                    unpublished.append({
                        "venue": venue,
                        "race": race_no,
                        "reason": "ライン未取得・再確認",
                        "prediction_url": pred_url
                    })
                    continue

                if line not in TARGET_LINES:
                    _star2_trace(venue, race_no, "STOP_LINE", reason="対象外ライン", line=line)
                    _race_diag(
                        venue, race_no, grade=grade, star=ai_star,
                        line=line, groups=groups, result="SKIP_LINE"
                    )
                    continue

                _star2_trace(venue, race_no, "ORDER_START", line=line, groups=groups)
                order3 = three_line_order(groups, hon, tai, ana, ren)
                _star2_trace(venue, race_no, "ORDER_READ", order3=order3 or "-")

                if not order3:
                    _race_diag(
                        venue, race_no, grade=grade, star=ai_star,
                        line=line, groups=groups, result="WAIT_ORDER"
                    )
                    _star2_trace(venue, race_no, "STOP_ORDER", reason="3人ライン印判定不能")
                    unpublished.append({
                        "venue": venue,
                        "race": race_no,
                        "reason": "3人ラインの印判定待ち",
                        "prediction_url": pred_url
                    })
                    continue

                if order3 not in TARGET_ORDERS:
                    _star2_trace(venue, race_no, "STOP_ORDER", reason="印条件対象外", order3=order3)
                    _race_diag(
                        venue, race_no, grade=grade, star=ai_star,
                        line=line, order3=order3, hon=hon, tai=tai,
                        ana=ana or "-", ren=ren or "-", result="SKIP_ORDER"
                    )
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

                _race_diag(
                    venue, race_no, grade=grade, star=ai_star,
                    line=line, order3=order3, result="MATCH"
                )
                _star2_trace(venue, race_no, "MATCH", line=line, order3=order3)
                _star2_diag(venue, race_no, "MATCH", line=line, order3=order3)
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
                msg = f"{venue} {race_no}R: {type(e).__name__}: {e}"
                _log(f"RACE_EXCEPTION {msg}")
                _log(f"URL {pred_url}")
                try:
                    _star2_trace(venue, race_no, "EXCEPTION", error=type(e).__name__, detail=str(e)[:180])
                except Exception:
                    pass

                if _looks_unpublished(pred_body):
                    unpublished.append({
                        "venue": venue,
                        "race": race_no,
                        "reason": f"予想・ライン取得途中で再確認 ({type(e).__name__})",
                        "prediction_url": pred_url
                    })
                else:
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
        try:
            if browser:
                browser.close()
        except Exception:
            pass

    _log(f"END venue={venue} checked={checked} matches={len(matches)} unpublished={len(unpublished)} errors={len(errors)}")
    return matches, unpublished, errors, checked


def scan_today(progress=None):
    _log(f"ENGINE_VERSION {ENGINE_VERSION}")
    import gc
    import time as _time

    today = _today_jst()

    all_matches = []
    all_unpublished = []
    all_errors = []
    checked_total = 0

    with sync_playwright() as p:
        venue_items = _discover_today_venues(p, today, progress=progress)
        if not venue_items:
            # 日別ページの構造変更・一時障害時だけ安全側で従来方式へフォールバック。
            _log("DISCOVER_EMPTY fallback=all_venues")
            venue_items = list(VENUES.items())

        venue_total = len(venue_items)
        _log(f"SCAN_TARGETS venues={venue_total}")

        for vi, (venue, slug) in enumerate(venue_items, 1):
            if progress:
                progress({
                    "phase": "venues",
                    "current": f"{venue} ({vi}/{venue_total})",
                    "done": vi - 1,
                    "total": venue_total
                })

            try:
                matches, unpublished, errors, checked = _process_one_venue(
                    p,
                    venue,
                    slug,
                    today,
                    progress=progress,
                    venue_index=vi - 1,
                    venue_total=venue_total
                )

                all_matches.extend(matches)
                all_unpublished.extend(unpublished)
                all_errors.extend(errors)
                checked_total += checked

            except Exception as e:
                _log(f"RACE_EXCEPTION venue={venue} race={race_no}R error={type(e).__name__}: {e}")
                msg = f"{venue}: {type(e).__name__}: {e}"
                _log(f"VENUE_CRASH {msg}")
                all_errors.append(msg)

            gc.collect()
            _time.sleep(0.25)

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
