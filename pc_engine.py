# -*- coding: utf-8 -*-
import re
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "https://www.winticket.jp"
ENGINE_VERSION="pc-bridge-v1.4.1-no-icon-is-day"
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




def _open_session(playwright):
    """Create an isolated Chromium session. A venue uses its own session so one crash cannot poison all later venues."""
    browser = playwright.chromium.launch(headless=True, args=_browser_args())
    context = browser.new_context(
        locale="ja-JP", viewport={"width": 900, "height": 700}, service_workers="block"
    )
    enable_fast_mode(context)
    page = context.new_page()
    page.set_default_timeout(5000)
    return browser, context, page


def _close_session(browser=None, context=None, page=None):
    """Best-effort cleanup; cleanup failures must never abort a completed scan."""
    for obj in (page, context, browser):
        if obj is None:
            continue
        try:
            obj.close()
        except Exception:
            pass


def _page_unhealthy(page):
    try:
        return page is None or page.is_closed()
    except Exception:
        return True

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


def goto_board(page, url, stop_event=None):
    """
    開催場一覧/開催場メタ情報専用。
    Render上では6秒だとWINTICKETのDOMContentLoaded待ちが間に合わないことがあるため、
    ここだけ最大12秒にする。レース巡回側の goto() は従来どおり6秒のまま。
    タイムアウトしても本文が取れていれば成功扱いにし、同一URLの自動再試行はしない。
    """
    _check_stop(stop_event)
    timeout_ms = 9000
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        _check_stop(stop_event)
        page.wait_for_timeout(150)
        _check_stop(stop_event)
        body = _body_text(page)
        if body:
            return body
        raise RuntimeError("ページ本文を取得できません")
    except StopRequested:
        raise
    except Exception as e:
        _check_stop(stop_event)
        body = _body_text(page)
        if body:
            _log(
                f"BOARD_GOTO_PARTIAL_OK attempt=1/1 timeout_ms={timeout_ms} "
                f"url={url} error={type(e).__name__}:{e}"
            )
            return body
        _log(
            f"BOARD_GOTO_FAIL attempt=1/1 timeout_ms={timeout_ms} "
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



def parse_board_grade(text):
    """
    競輪トップ「本日開催中の競輪場」カード専用。
    この一覧では F1/F2 だけを級として採用し、選手情報などの L1/L級は無視する。
    """
    if not text:
        return ""
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


def is_girls_text(text):
    """
    レース単位のガールズ判定専用。
    開催場のF1/F2判定には使わない。
    """
    if not text:
        return False
    return bool(
        re.search(r"(?<![A-Z0-9])L1(?![A-Z0-9])", text)
        or "L級" in text
        or "ガールズ" in text
        or "女子" in text
    )


def is_girls_race(page, race_no):
    """
    チェックしたF2場の各レースを開いた後、そのレースがガールズかを最初に判定する。

    方針:
    - 「○R」を含む小さい局所DOMを優先する。
    - その局所DOM内の L1 / L級 / ガールズ / 女子 を見る。
    - ページ全体の別レースや注目選手の L1 は使わない。
    - ガールズと確認できた時だけ True。判定不能を勝手にガールズ扱いしない。
    """
    try:
        texts = page.evaluate(r"""(raceNo)=>{
          const key=String(raceNo)+'R';
          const out=[];
          const nodes=[...document.querySelectorAll(
            'h1,h2,h3,[class*="Header"],[class*="Title"],[class*="RaceInfo"],[class*="RaceHeader"],[class*="RaceCard"]'
          )];

          const add=(t)=>{
            t=(t||'').trim();
            if(!t || t.length>420 || !t.includes(key)) return;
            if(!out.includes(t)) out.push(t);
          };

          for(const e of nodes){
            add(e.innerText||e.textContent||'');
            let n=e.parentElement;
            for(let k=0;k<4 && n;k++,n=n.parentElement){
              const t=(n.innerText||n.textContent||'').trim();
              if(t.length>520) break;
              add(t);
            }
            if(out.length>=24) break;
          }

          out.sort((a,b)=>a.length-b.length);
          return out;
        }""", race_no) or []

        for t in texts:
            if is_girls_text(t):
                return True, t

        return False, texts[0] if texts else ""
    except Exception:
        return False, ""


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



def _session(text):
    text=text or ""
    if "ミッドナイト" in text:return "MN"
    if "ナイター" in text:return "N"
    if "モーニング" in text:return "M"
    times=[int(h)*60+int(m) for h,m in re.findall(r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)",text)]
    if times:
        if max(times)>=21*60:return "MN"
        if max(times)>=18*60:return "N"
        if min(times)<=10*60:return "M"
    return "D"

def _rgb_to_hue(rgb_text):
    """
    CSS rgb()/rgba() 文字列を hue(0-360), saturation(0-1), lightness(0-1) に変換。
    解析不能/透明色は None。
    """
    if not rgb_text:
        return None
    m = re.search(
        r"rgba?\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)"
        r"(?:\s*,\s*(\d+(?:\.\d+)?))?\s*\)",
        str(rgb_text)
    )
    if not m:
        return None
    r, g, b = [float(m.group(i)) / 255.0 for i in (1,2,3)]
    a = float(m.group(4)) if m.group(4) is not None else 1.0
    if a <= 0.05:
        return None

    mx, mn = max(r,g,b), min(r,g,b)
    d = mx - mn
    l = (mx + mn) / 2.0
    if d == 0:
        h = 0.0
        s = 0.0
    else:
        s = d / (1.0 - abs(2.0*l - 1.0)) if l not in (0.0,1.0) else 0.0
        if mx == r:
            h = 60.0 * (((g-b)/d) % 6.0)
        elif mx == g:
            h = 60.0 * (((b-r)/d) + 2.0)
        else:
            h = 60.0 * (((r-g)/d) + 4.0)
    return h, s, l


def _session_from_icon_meta(icon_meta):
    """
    WINTICKETの開催カードに表示される時間帯アイコンを判定。

    優先順位:
      太陽/朝/Morning -> M
      月/Night       -> N
      夜空/Midnight  -> MN
      ハート/Girls   -> 時間帯判定には使わない

    HTML側に意味のある alt/title/aria-label/use href/class/data-* があれば最優先。
    意味ラベルが無いSVGでも、アイコン専用バッジの背景色を補助的に使う。
    F1/F2/G1-G3の級バッジはテキストを持つため色判定対象から除外済み。
    """
    metas = icon_meta or []

    # 1) Semantic attributes / filenames / SVG use href.
    for meta in metas:
        token = " ".join(str(meta.get(k,"")) for k in (
            "alt","title","aria","src","use","cls","data","text"
        )).lower()

        # Heart/girls is NOT a session marker.
        if any(k in token for k in ("heart","girl","girls","ガールズ","女子","favorite","pink")):
            continue

        if any(k in token for k in (
            "midnight","mid-night","night-sky","nightsky","夜空","ミッドナイト"
        )):
            return "MN"
        if any(k in token for k in (
            "morning","sun","sunny","daybreak","朝","モーニング","太陽"
        )):
            return "M"
        if any(k in token for k in (
            "moon","night","night-race","ナイター","月"
        )):
            return "N"

    # 2) Color fallback for icon-only badges.
    # WINTICKET screenshots/DOM styling:
    #   orange/yellow icon -> morning
    #   blue/cyan icon     -> nighter
    #   violet/purple icon -> midnight
    #   pink/red icon      -> heart/girls (ignore)
    candidates=[]
    for meta in metas:
        for color_key in ("background","color"):
            hsl = _rgb_to_hue(meta.get(color_key))
            if not hsl:
                continue
            h,s,l = hsl
            if s < 0.30 or l < 0.15 or l > 0.90:
                continue
            candidates.append((h,s,l,meta))

    for h,s,l,meta in candidates:
        # pink/red = heart/girls. Never turn this into D/M/N/MN.
        if h >= 320 or h <= 8:
            continue
        if 8 < h < 70:
            return "M"
        if 185 <= h < 255:
            return "N"
        if 255 <= h < 320:
            return "MN"

    return ""


def _session_from_live_card(text, icon_meta=None, icon_scan_ok=False):
    """
    開催区分は同じ開催カード内の時間帯アイコンを最優先する。

      太陽  -> M
      月    -> N
      夜空  -> MN
      上記3つが無い -> D

    ハートはガールズ戦を含む印なので時間帯判定には一切使わない。
    ハートがあっても無くても、太陽/月/夜空が無ければ D。

    icon_scan_ok=True は「対象カードのアイコン領域を正常に確認できた」ことを意味する。
    その場合、icon_meta が空でも D と確定する。
    カード自体のアイコン走査に失敗した場合だけ、時刻から補助推定する。
    """
    icon_meta = icon_meta or []

    session = _session_from_icon_meta(icon_meta)
    if session:
        return session

    # カード内のアイコンDOMを正常に確認済みなら、
    # 太陽/月/夜空が見つからない = デイ。
    # ハートの有無は関係ない。
    if icon_scan_ok:
        return "D"

    # アイコン走査そのものができなかった場合だけ時刻で補助推定。
    text = text or ""
    tm = re.search(r"(?:発走|締切)\s*([0-2]?\d):([0-5]\d)", text)
    rm = re.search(r"(?<!\d)(1[0-2]|[1-9])R(?!\d)", text)
    if not tm:
        return "?"

    minutes = int(tm.group(1)) * 60 + int(tm.group(2))
    race_no = int(rm.group(1)) if rm else 1
    estimated_first = minutes - max(0, race_no - 1) * 25

    if estimated_first >= 19 * 60:
        return "MN"
    if estimated_first >= 14 * 60:
        return "N"
    if estimated_first <= 10 * 60:
        return "M"
    return "D"


def _live_card_info_from_anchor(anchor, venue):
    """
    「本日開催中の競輪場」の1カードだけを読む。
    会場名/F1-F2と、同じカード内のアイコンDOM情報を返す。

    隣カード・別日カード・注目選手の要素は見ない。
    """
    try:
        venue_names = list(VENUES.keys())
        return anchor.evaluate(r"""(e,args)=>{
          const venue=args.venue;
          const venueNames=args.venueNames||[];
          const candidates=[];

          const venueCount=(t)=>{
            let n=0;
            for(const v of venueNames){
              if(v && t.includes(v)) n++;
              if(n>=2) break;
            }
            return n;
          };

          let n=e;
          for(let k=0;k<9 && n;k++,n=n.parentElement){
            const t=(n.innerText||n.textContent||'').trim();
            if(!t || t.length>650) continue;
            if(!t.includes(venue)) continue;
            if(!/(?:^|[^A-Z0-9])F[12](?:[^A-Z0-9]|$)/.test(t)) continue;
            if(venueCount(t)>=2) continue;
            candidates.push({node:n,text:t});
          }

          candidates.sort((a,b)=>a.text.length-b.text.length);
          const best=candidates[0];
          if(!best) return {text:'',icons:[],html:'',icon_scan_ok:false};

          const root=best.node;
          const icons=[];
          const all=[...root.querySelectorAll('*')];

          for(const el of all){
            const txt=(el.innerText||el.textContent||'').trim();

            // Grade badges have visible F1/F2/G1-G3 text and are not session icons.
            if(/^(?:F1|F2|G1|G2|G3)$/.test(txt)) continue;
            // Ignore ordinary text-heavy elements.
            if(txt.length>12) continue;

            const hasSvg=!!el.querySelector?.('svg');
            const hasImg=el.tagName==='IMG' || !!el.querySelector?.('img');
            const selfSvg=el.tagName==='SVG';
            const selfImg=el.tagName==='IMG';
            const aria=el.getAttribute?.('aria-label')||'';
            const title=el.getAttribute?.('title')||'';
            const cls=typeof el.className==='string'?el.className:
                      (el.getAttribute?.('class')||'');
            const data=[...(el.attributes||[])]
              .filter(a=>a.name.startsWith('data-'))
              .map(a=>a.name+'='+a.value).join(' ');

            if(!(hasSvg||hasImg||selfSvg||selfImg||aria||title||
                 /icon|badge|mark|moon|sun|night|heart|girl|morning|midnight/i.test(cls+' '+data))){
              continue;
            }

            const img=(selfImg?el:el.querySelector?.('img'));
            const svg=(selfSvg?el:el.querySelector?.('svg'));
            const use=svg?.querySelector?.('use');
            const style=getComputedStyle(el);

            // Ignore huge containers; session badges are small.
            const r=el.getBoundingClientRect();
            if(r.width>80 || r.height>80) continue;

            icons.push({
              text:txt,
              alt:img?.getAttribute('alt')||'',
              src:img?.getAttribute('src')||'',
              aria:aria || svg?.getAttribute('aria-label') || '',
              title:title || svg?.querySelector('title')?.textContent || '',
              use:use?.getAttribute('href') || use?.getAttribute('xlink:href') || '',
              cls:cls,
              data:data,
              background:style.backgroundColor||'',
              color:style.color||'',
              width:Math.round(r.width),
              height:Math.round(r.height)
            });
          }

          // De-duplicate nested wrappers describing the same icon.
          const seen=new Set();
          const unique=[];
          for(const x of icons){
            const key=[x.alt,x.src,x.aria,x.title,x.use,x.cls,x.data,x.background,x.color].join('|');
            if(seen.has(key)) continue;
            seen.add(key);
            unique.push(x);
          }

          return {
            text:best.text,
            icons:unique.slice(0,30),
            html:(root.innerHTML||'').slice(0,5000),
            icon_scan_ok:true
          };
        }""", {"venue": venue, "venueNames": venue_names}) or {"text":"","icons":[],"html":"","icon_scan_ok":False}
    except Exception:
        return {"text":"","icons":[],"html":"","icon_scan_ok":False}


def _venue_meta(page, slug, stop_event=None):
    """トップで級が取れない時だけ、公開の競輪場ページから本日の級/開催区分を補完する。"""
    url=f"{BASE}/keirin/{slug}"
    text=goto_board(page,url,stop_event)
    # 本日のレースの塊だけを優先し、「今月のレース」以降の別開催を混ぜない。
    local=text
    m=re.search(r"本日のレース(?P<body>.*?)(?:今月のレース|競輪場情報|$)", text, re.S)
    if m:
        local=m.group('body')
    grade=parse_grade(local)
    session=_session(local)
    return grade,session


def discover_today_board(page,today,stop_event,progress=None,counters=None):
    """
    WINTICKET /keirin の「本日開催中の競輪場」だけを取得する。

    v1.2:
    - top-page-recent-race-venue のリンクだけを採用。
    - 「開催中のレース」全体へのフォールバックを廃止
      （昨日/本日/明日の混入を防止）。
    - 各カードの最小DOMだけから会場名/F1/F2を読む。
    - トップページにF1/F2が見えないカードは「?」として返し、推測しない。
    """
    progress = progress or (lambda x: None)
    counters = counters if counters is not None else {}
    counters["board_stage"] = 1
    counters["board_done"] = 0
    counters["board_total"] = 0

    _progress(
        progress, counters,
        phase="board",
        current="WINTICKET競輪トップへ接続中",
        detail="① 「本日開催中の競輪場」だけを確認します。"
    )
    goto_board(page, f"{BASE}/keirin", stop_event)

    counters["board_stage"] = 2
    _progress(
        progress, counters,
        phase="board",
        current="本日開催場を抽出中",
        detail="② 今日の競輪場カードだけを読み込んでいます。"
    )

    discovered=[]
    seen=set()

    try:
        links=page.locator('a[href*="top-page-recent-race-venue"]')
        for i in range(links.count()):
            _check_stop(stop_event)
            a=links.nth(i)
            href=a.get_attribute("href") or ""
            m=re.search(r"/keirin/([^/?#]+)", href)
            if not m:
                continue
            slug=m.group(1)
            venue=next((name for name,sl in VENUES.items() if sl==slug), "")
            if not venue or slug in seen:
                continue
            seen.add(slug)

            card=_live_card_info_from_anchor(a, venue)
            card_text=card.get("text","") if isinstance(card,dict) else ""
            icon_meta=card.get("icons",[]) if isinstance(card,dict) else []
            icon_scan_ok=bool(card.get("icon_scan_ok",False)) if isinstance(card,dict) else False
            grade=parse_board_grade(card_text)
            session=_session_from_live_card(card_text, icon_meta, icon_scan_ok)

            discovered.append({
                "venue": venue,
                "slug": slug,
                "grade": grade or "?",
                "session": session,
                "card_text": card_text,
                "icon_meta": icon_meta,
            })
    except Exception as e:
        _log(f"BOARD_TODAY_CARDS_FAIL error={type(e).__name__}:{e}")

    if not discovered:
        raise RuntimeError("「本日開催中の競輪場」カードを取得できません")

    counters["board_total"] = len(discovered)
    counters["board_stage"] = 3

    out=[]
    for idx,item in enumerate(discovered,1):
        _check_stop(stop_event)
        row={
            "venue": item["venue"],
            "slug": item["slug"],
            "grade": item["grade"],
            "session": item["session"],
        }
        out.append(row)
        counters["board_done"] = idx
        _progress(
            progress, counters,
            phase="board",
            current=f"{item['venue']} を確認 ({idx}/{len(discovered)}場)",
            detail=f"F1/F2：{row['grade']}　開催区分：{row['session']}"
        )
        _log(
            f"BOARD_CARD venue={item['venue']} slug={item['slug']} "
            f"grade={row['grade']} session={row['session']} "
            f"text={repr((item.get('card_text') or '')[:180])} "
            f"icons={repr((item.get('icon_meta') or [])[:8])}"
        )

    return out


def get_today_races(page,slug,today,stop_event):
    goto(page,f"{BASE}/keirin/{slug}/raceresult/{today.year}{today.month:02d}",stop_event)
    rows=[];seen=set();links=page.locator("a[href*='/raceresult/']")
    for i in range(links.count()):
        href=links.nth(i).get_attribute("href") or ""; info=race_info(href)
        if not info:continue
        sid,day,race,actual=info
        if actual!=today or (sid,day,race) in seen:continue
        seen.add((sid,day,race)); rows.append({"sid":sid,"day":day,"race":race})
    return sorted(rows,key=lambda x:x["race"])

def _progress(progress,counters,**kw):
    kw["counters"]=dict(counters);progress(kw)

def load_today_board(progress=None,stop_event=None):
    progress=progress or (lambda x:None); today=_today_jst(); c={"venues_found":0,"errors":0,"board_stage":0,"board_done":0,"board_total":0}
    browser=context=page=None
    try:
      with sync_playwright() as p:
        try:
          browser,context,page=_open_session(p)
          _progress(progress,c,phase="board",current="今日の開催場を取得中",detail="「本日開催中の競輪場」カードだけで開催場・F1/F2を確認")
          board=discover_today_board(page,today,stop_event,progress,c);c["venues_found"]=len(board);c["board_done"]=len(board);c["board_total"]=len(board)
          _progress(progress,c,phase="select",current="開催場を選択",detail="検索したい場にチェック",venues_info=board)
          return {"venues_info":board,"counters":c,"errors":[],"stopped":False}
        finally:
          _close_session(browser,context,page)
    except StopRequested:
      return {"venues_info":[],"counters":c,"errors":[],"stopped":True}
    except Exception as e:
      c["errors"] += 1
      msg=f"開催場取得: {type(e).__name__}: {e}"
      _log(f"BOARD_LOAD_FAIL error={type(e).__name__}:{e}")
      _progress(progress,c,phase="error",current="開催場取得エラー",detail=msg,venues_info=[])
      return {"venues_info":[],"counters":c,"errors":[msg],"stopped":False}

def scan_selected(selected,board,progress=None,stop_event=None):
    progress=progress or (lambda x:None);today=_today_jst();selected=set(selected or [])
    infos={x["slug"]:x for x in board or []}
    # UIだけでなく取得エンジン側でもF2を強制。F1/?がselectedに混ざっても検索しない。
    jobs=[x for x in (board or []) if x.get("slug") in selected and x.get("grade")=="F2"]
    c={"venues_found":len(board or []),"selected_venues":len(jobs),"checked_races":0,"girls_l":0,"unpublished":0,"f2_races":0,"star2":0,"line_target":0,"order_target":0,"matched":0,"errors":0,"browser_restarts":0,"venue_index":0}
    result={"matches":[],"errors":[],"skipped":[],"counters":c,"venues_info":board or [],"stopped":False}
    try:
      with sync_playwright() as p:
        for vi,info in enumerate(jobs,1):
          _check_stop(stop_event)
          venue,slug=info["venue"],info["slug"]
          browser=context=page=None
          try:
            browser,context,page=_open_session(p)
            _progress(progress,c,phase="races",current=f"{venue} ({vi}/{len(jobs)})",detail=f"選択{len(jobs)}場中{vi}場目",matches=list(result["matches"]),venues_info=board)
            try:
              rows=get_today_races(page,slug,today,stop_event)
            except StopRequested:
              raise
            except Exception as e:
              c["errors"]+=1;result["errors"].append(f"{venue}: {type(e).__name__}: {e}")
              _log_venue(venue,"RACE_LIST_FAIL",error=f"{type(e).__name__}:{e}")
              continue

            for row in rows:
              _check_stop(stop_event);race=row["race"];url=f"{BASE}/keirin/{slug}/predictions/{row['sid']}/{row['day']}/{race}"
              _progress(progress,c,phase="races",current=f"{venue} {race}R",detail=f"{venue}を巡回中",matches=list(result["matches"]),venues_info=board)
              try:
                goto(page,url,stop_event);c["checked_races"]+=1

                # ① ガールズ戦を最初に排除。
                girls,girls_source=is_girls_race(page,race)
                if girls:
                    c["girls_l"]+=1
                    result["skipped"].append({
                        "venue":venue,
                        "race":race,
                        "reason":"ガールズ戦を除外"
                    })
                    _progress(
                        progress,c,
                        phase="races",
                        current=f"{venue} {race}R",
                        detail="ガールズ戦 → 除外",
                        matches=list(result["matches"]),
                        venues_info=board
                    )
                    _log_race(venue,race,"SKIP_GIRLS",source=repr((girls_source or "")[:180]))
                    continue

                # ② 男子レースだけ星2を確認。
                c["f2_races"]+=1
                _progress(progress,c,phase="races",current=f"{venue} {race}R",detail="男子F2 → 星2を確認中",matches=list(result["matches"]),venues_info=board)
                star,_=confidence_dom(page)
                if star is None:
                    page.wait_for_timeout(180)
                    star,_=confidence_dom(page)
                if star is None:
                    c["unpublished"]+=1
                    result["skipped"].append({"venue":venue,"race":race,"reason":"AI予想未発表"})
                    continue
                if star!=TARGET_STAR:
                    continue

                # ③ 星2だけ指定ラインを確認。
                c["star2"]+=1
                _progress(progress,c,phase="races",current=f"{venue} {race}R",detail="星2 → 指定ラインを確認中",matches=list(result["matches"]),venues_info=board)
                line,groups=extract_line(page,stop_event)
                if line not in TARGET_LINES:
                    continue

                # ④ 指定ラインだけ印順を確認。
                c["line_target"]+=1
                _progress(progress,c,phase="races",current=f"{venue} {race}R",detail="指定ライン → ◎○△ / ◎○× を確認中",matches=list(result["matches"]),venues_info=board)
                hon,tai,ana,ren=extract_ai_marks(page)
                if hon in ("",None) or tai in ("",None):
                    continue
                order3=three_line_order(groups,hon,tai,ana,ren)
                if order3 not in TARGET_ORDERS:
                    continue
                c["order_target"]+=1
                result["matches"].append({
                    "venue": venue,
                    "slug": slug,
                    "race": race,
                    "session": info.get("session", "?"),
                    "grade": info.get("grade", "?"),
                    "star": "星2",
                    "line": line,
                    "three_order": order3,
                    "order": line_mark_order(groups,hon,tai,ana,ren),
                    "prediction_url": url,
                    "winticket_url": url,
                    "event_url": f"{BASE}/keirin/{slug}/racecard/{row['sid']}",
                }); c["matched"] += 1
                _progress(progress,c,phase="races",current=f"{venue} {race}R",detail="条件一致",matches=list(result["matches"]),venues_info=board)
              except StopRequested:
                raise
              except Exception as e:
                c["errors"]+=1;result["errors"].append(f"{venue} {race}R: {type(e).__name__}: {e}")
                _log_race(venue,race,"ERROR",error=f"{type(e).__name__}:{e}")
                # If Chromium/page died, replace the whole session before the next race.
                # We intentionally do NOT retry the same URL, preserving the no-duplicate-access rule.
                if _page_unhealthy(page) or "Target page, context or browser has been closed" in str(e) or "Browser has been closed" in str(e):
                  _close_session(browser,context,page); browser=context=page=None
                  try:
                    browser,context,page=_open_session(p); c["browser_restarts"]+=1
                    _log_venue(venue,"BROWSER_RECOVERED",after_race=race)
                  except Exception as rexc:
                    c["errors"]+=1; result["errors"].append(f"{venue} ブラウザ再起動失敗: {type(rexc).__name__}: {rexc}")
                    _log_venue(venue,"BROWSER_RECOVERY_FAIL",error=f"{type(rexc).__name__}:{rexc}")
                    break
          finally:
            _close_session(browser,context,page)

        _progress(progress,c,phase="done",current="検索完了",detail="選択した場の検索完了",matches=list(result["matches"]),venues_info=board)
        return result
    except StopRequested:
      result["stopped"]=True;_progress(progress,c,phase="stopped",current="途中停止",detail="停止しました",matches=list(result["matches"]),venues_info=board);return result