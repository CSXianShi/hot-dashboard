#!/usr/bin/env python3
"""樾的 F1 + 歌星热点仪表盘生成器。

抓取 RSS/API -> 计算热度 -> 生成静态 index.html。
纯标准库,无依赖;适合在 1GB 小服务器上跑。

v3:每卡片 SVG sparkline、信源扩展(站点 RSS + 别名归属)、热度分 v2(基础信号 + 趋势加成)。
"""
import json
import re
import html
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
HISTORY = BASE / "history.json"
UA = {"User-Agent": "Mozilla/5.0 (Linux; dashboard-bot)"}
CST = timezone(timedelta(hours=8))
WEEKDAY = "一二三四五六日"

# 单源超时/重试:批量信源保持轻量,单源 ≤10s(方案约束)
FEED_TIMEOUT = 10
FEED_RETRIES = 1

F1_FEEDS = [
    ("Autosport", "https://www.autosport.com/rss/f1/news/"),
    ("Motorsport", "https://www.motorsport.com/rss/f1/news/"),
    ("F1.com", "https://www.formula1.com/en/latest/all.xml"),
    ("The Race", "https://the-race.com/feed/"),
    ("RaceFans", "https://www.racefans.net/feed/"),
    ("PlanetF1", "https://www.planetf1.com/rss"),
]

ARTISTS = [
    "Olivia Rodrigo",
    "Ariana Grande",
    "Charli XCX",
    "Tate McRae",
    "Billie Eilish",
    "RAYE",
]

# 全站信源:每个只抓一次,再按歌手名(词边界)归属,避免逐人重复抓取
ARTIST_FEEDS = [
    ("Billboard", "https://www.billboard.com/feed/"),
    ("Rolling Stone", "https://www.rollingstone.com/music/feed/"),
    ("NME", "https://www.nme.com/feed"),
    ("Pitchfork", "https://pitchfork.com/feed/feed-news/rss"),
]

# 归属匹配的别名(词边界、大小写不敏感)。RAYE 是短词,必须词边界防假阳性。
ARTIST_ALIASES = {
    "Olivia Rodrigo": [r"olivia rodrigo"],
    "Ariana Grande": [r"ariana grande"],
    "Charli XCX": [r"charli xcx"],
    "Tate McRae": [r"tate mcrae"],
    "Billie Eilish": [r"billie eilish"],
    "RAYE": [r"raye"],
}
ARTIST_PATTERNS = {
    name: [re.compile(r"\b" + p + r"\b", re.I) for p in pats]
    for name, pats in ARTIST_ALIASES.items()
}

GNEWS = 'https://news.google.com/rss/search?q="{q}"&hl=en-US&gl=US&ceid=US:en'

SESSION_NAMES = [
    ("FirstPractice", "一练 FP1"),
    ("SecondPractice", "二练 FP2"),
    ("ThirdPractice", "三练 FP3"),
    ("SprintQualifying", "冲刺排位"),
    ("Sprint", "冲刺赛"),
    ("Qualifying", "排位赛"),
]

ATOM = "{http://www.w3.org/2005/Atom}"


def fetch(url, timeout=20, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(2 * (i + 1))
    raise last


def _parse_ts(pub):
    try:
        ts = parsedate_to_datetime(pub)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, TypeError):
        return None


def parse_rss(raw):
    """Return list of {title, link, ts} from RSS <item> 或 Atom <entry>。"""
    items = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return items
    # RSS 2.0
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        ts = _parse_ts(it.findtext("pubDate") or "")
        if title:
            items.append({"title": title, "link": link, "ts": ts})
    # Atom(部分源用 <entry>)
    for it in root.iter(ATOM + "entry"):
        title = (it.findtext(ATOM + "title") or "").strip()
        link = ""
        for ln in it.iter(ATOM + "link"):
            if ln.get("rel", "alternate") in ("alternate", ""):
                link = ln.get("href", "")
                break
        pub = it.findtext(ATOM + "updated") or it.findtext(ATOM + "published") or ""
        ts = _parse_ts(pub)
        if title:
            items.append({"title": title, "link": link, "ts": ts})
    return items


def hours_ago(ts, now):
    if ts is None:
        return 999
    return (now - ts).total_seconds() / 3600


def norm_title(t):
    return re.sub(r"\W+", "", t.lower())[:80]


def cst_str(date, t):
    dt = datetime.fromisoformat(date + "T" + (t or "12:00:00Z").replace("Z", "+00:00")).astimezone(CST)
    return dt, f"{dt.month}月{dt.day}日(周{WEEKDAY[dt.weekday()]}) {dt:%H:%M}"


def collect_f1(now):
    news, seen = [], set()
    for src, url in F1_FEEDS:
        try:
            for it in parse_rss(fetch(url, timeout=FEED_TIMEOUT, retries=FEED_RETRIES)):
                key = norm_title(it["title"])[:60]
                if key in seen:
                    continue
                seen.add(key)
                it["src"] = src
                it["age"] = hours_ago(it["ts"], now)
                news.append(it)
        except Exception as e:
            print(f"[warn] F1 feed {src}: {e}")
    news.sort(key=lambda x: x["age"])
    return [n for n in news if n["age"] < 72][:8]


def collect_standings(now):
    out = {"drivers": [], "constructors": [], "next_race": None}
    try:
        d = json.loads(fetch("https://api.jolpi.ca/ergast/f1/current/driverstandings.json", timeout=12, retries=1))
        for r in d["MRData"]["StandingsTable"]["StandingsLists"][0]["DriverStandings"][:8]:
            out["drivers"].append({
                "pos": r["position"],
                "name": r["Driver"]["givenName"] + " " + r["Driver"]["familyName"],
                "team": r["Constructors"][0]["name"] if r["Constructors"] else "",
                "pts": r["points"],
            })
    except Exception as e:
        print(f"[warn] driver standings: {e}")
    try:
        d = json.loads(fetch("https://api.jolpi.ca/ergast/f1/current/constructorstandings.json", timeout=12, retries=1))
        for r in d["MRData"]["StandingsTable"]["StandingsLists"][0]["ConstructorStandings"][:5]:
            out["constructors"].append({"pos": r["position"], "name": r["Constructor"]["name"], "pts": r["points"]})
    except Exception as e:
        print(f"[warn] constructor standings: {e}")
    try:
        d = json.loads(fetch("https://api.jolpi.ca/ergast/f1/current/races.json", timeout=12, retries=1))
        today = now.date()
        for r in d["MRData"]["RaceTable"]["Races"]:
            rd = datetime.strptime(r["date"], "%Y-%m-%d").date()
            if rd >= today:
                race_dt, race_s = cst_str(r["date"], r.get("time"))
                sessions = []
                for key, label in SESSION_NAMES:
                    s = r.get(key)
                    if s:
                        dt, txt = cst_str(s["date"], s.get("time"))
                        sessions.append({"label": label, "txt": txt, "past": dt < now})
                sessions.append({"label": "正赛 🏁", "txt": race_s, "past": race_dt < now})
                out["next_race"] = {
                    "name": r["raceName"],
                    "circuit": r["Circuit"]["circuitName"],
                    "round": r["round"],
                    "days": (rd - today).days,
                    "sessions": sessions,
                }
                break
    except Exception as e:
        print(f"[warn] race schedule: {e}")
    return out


def match_artist(title):
    """返回该标题命中的歌手名,未命中返回 None。"""
    for name, pats in ARTIST_PATTERNS.items():
        if any(p.search(title) for p in pats):
            return name
    return None


def collect_artists(now):
    # 每位歌手一个 item 池(先 Google News,再全站源归属),按标题去重
    pools = {name: {} for name in ARTISTS}  # name -> {norm_title: item}

    def add(name, it, src):
        key = norm_title(it["title"])
        if not key or key in pools[name]:
            return
        pools[name][key] = {"title": it["title"], "link": it["link"], "ts": it["ts"], "src": src}

    # 1) 每位歌手的 Google News 精确查询
    for name in ARTISTS:
        try:
            for it in parse_rss(fetch(GNEWS.format(q=urllib.request.quote(name)),
                                      timeout=FEED_TIMEOUT, retries=FEED_RETRIES)):
                add(name, it, "Google News")
        except Exception as e:
            print(f"[warn] artist gnews {name}: {e}")

    # 2) 全站信源:每源抓一次,按名字归属
    for src, url in ARTIST_FEEDS:
        try:
            items = parse_rss(fetch(url, timeout=FEED_TIMEOUT, retries=FEED_RETRIES))
        except Exception as e:
            print(f"[warn] artist feed {src}: {e}")
            continue
        for it in items:
            name = match_artist(it["title"])
            if name:
                add(name, it, src)

    rows = []
    for name in ARTISTS:
        items = list(pools[name].values())
        n24 = sum(1 for it in items if hours_ago(it["ts"], now) < 24)
        n48 = sum(1 for it in items if hours_ago(it["ts"], now) < 48)
        base = n24 * 2 + n48  # 纯信号:存进 history,趋势据此计算,无反馈回路
        fresh = sorted(items, key=lambda x: hours_ago(x["ts"], now))
        rows.append({
            "name": name, "n24": n24, "n48": n48, "score": base,
            "top": [{"title": i["title"], "link": i["link"], "src": i["src"]} for i in fresh[:3]],
        })
    return rows


def load_history():
    try:
        return json.loads(HISTORY.read_text())
    except Exception:
        return []


def update_history(artists, now):
    """追加本次基础分快照;返回 (上一次 {name: base}, 含本次的完整 history)。"""
    hist = load_history()
    prev = hist[-1]["scores"] if hist else {}
    hist.append({"ts": now.isoformat(timespec="minutes"), "scores": {a["name"]: a["score"] for a in artists}})
    hist = hist[-60:]
    HISTORY.write_text(json.dumps(hist, ensure_ascii=False, indent=1))
    return prev, hist


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def trend_mark(delta):
    if delta is None:
        return ""
    if delta > 0:
        return f'<b class="up">↑{delta}</b>'
    if delta < 0:
        return f'<b class="down">↓{-delta}</b>'
    return '<b class="flat">→</b>'


def sparkline(series):
    """series: 时间顺序的基础分列表。≥3 点才画,否则返回 ""(调用方降级为箭头)。"""
    series = series[-14:]
    if len(series) < 3:
        return ""
    w, h, pad = 132, 30, 3
    lo, hi = min(series), max(series)
    rng = (hi - lo) or 1
    n = len(series)
    pts = []
    for i, v in enumerate(series):
        x = pad + (w - 2 * pad) * i / (n - 1)
        y = pad + (h - 2 * pad) * (1 - (v - lo) / rng)
        pts.append((x, y))
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    lx, ly = pts[-1]
    cur = series[-1]
    return (
        f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'preserveAspectRatio="xMinYMid meet" aria-hidden="true">'
        f'<polyline points="{poly}" fill="none" stroke="#f778ba" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.4" fill="#ffa657"/>'
        f'<text x="{lx - 4:.1f}" y="{ly - 4:.1f}" text-anchor="end" '
        f'font-size="9" fill="#ffa657">{cur}</text></svg>'
    )


def esc(s):
    return html.escape(s, quote=True)


def render(f1_news, standings, artists, prev_scores, hist, now):
    updated = now.astimezone(CST).strftime("%Y-%m-%d %H:%M")
    nr = standings["next_race"]
    next_html = ""
    if nr:
        srows = "".join(
            f'<tr class="{"past" if s["past"] else ""}"><td>{s["label"]}</td><td>{esc(s["txt"])}</td></tr>'
            for s in nr["sessions"]
        )
        countdown = "今天就是比赛日!" if nr["days"] == 0 else f'距正赛还有 <b>{nr["days"]}</b> 天'
        next_html = (
            f'<div class="nextrace">🏁 第{nr["round"]}站:<b>{esc(nr["name"])}</b>'
            f'<span class="dim">{esc(nr["circuit"])}</span>'
            f'<div class="small" style="margin:4px 0">{countdown} · 以下均为北京时间</div>'
            f'<table>{srows}</table></div>'
        )

    drows = "".join(
        f'<tr><td>{d["pos"]}</td><td>{esc(d["name"])}</td>'
        f'<td class="dim">{esc(d["team"])}</td><td class="pts">{d["pts"]}</td></tr>'
        for d in standings["drivers"]
    )
    crows = "".join(
        f'<tr><td>{c["pos"]}</td><td>{esc(c["name"])}</td><td class="pts">{c["pts"]}</td></tr>'
        for c in standings["constructors"]
    )
    nrows = "".join(
        f'<li><a href="{esc(n["link"])}" target="_blank" rel="noopener">{esc(n["title"])}</a>'
        f'<span class="dim"> · {esc(n["src"])} · {int(n["age"])}h前</span></li>'
        for n in f1_news
    )

    # 计算显示分:composite = base + clamp(base - prev_base, ±10),history 只存 base
    for a in artists:
        base = a["score"]
        pv = prev_scores.get(a["name"])
        a["delta"] = None if pv is None else clamp(base - pv, -10, 10)
        a["display"] = base + (a["delta"] or 0)
    artists.sort(key=lambda x: -x["display"])

    max_score = max((a["display"] for a in artists), default=0) or 1
    acards = ""
    for a in artists:
        bar = int(max(0, a["display"]) / max_score * 100)
        fire = "🔥" * min(3, 1 + a["display"] // (max_score // 2 + 1)) if a["display"] > 0 else "💤"
        trend = trend_mark(a["delta"])
        series = [s["scores"][a["name"]] for s in hist if a["name"] in s["scores"]]
        spark = sparkline(series)
        spark_html = f'<div class="sparkrow">{spark}</div>' if spark else ""
        links = "".join(
            f'<li><a href="{esc(t["link"])}" target="_blank" rel="noopener">{esc(t["title"])}</a>'
            f'<span class="dim"> · {esc(t["src"])}</span></li>'
            for t in a["top"]
        )
        acards += f"""<div class="card artist">
<div class="ahead"><b>{esc(a["name"])}</b><span>{fire} 热度 {a["display"]} {trend}</span></div>
<div class="bar"><i style="width:{bar}%"></i></div>
{spark_html}<div class="dim small">24h 内 {a["n24"]} 条报道 · 48h 内 {a["n48"]} 条</div>
<ul class="small">{links}</ul></div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>樾的热点仪表盘</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; margin: 0; }}
body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  background: #0d1117; color: #e6edf3; padding: 16px; max-width: 760px; margin: auto;
  -webkit-text-size-adjust: 100%; }}
h1 {{ font-size: 1.3em; margin-bottom: 2px; }}
h2 {{ font-size: 1.05em; margin: 18px 0 8px; color: #79c0ff; }}
.dim {{ color: #8b949e; }} .small {{ font-size: .85em; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px;
  padding: 12px 14px; margin-bottom: 10px; }}
.nextrace {{ background: linear-gradient(90deg,#2d1a1a,#161b22); border: 1px solid #6e2c2c;
  border-radius: 10px; padding: 12px 14px; margin: 10px 0; }}
.nextrace .dim {{ margin-left: 8px; font-size: .85em; }}
.nextrace tr.past td {{ color: #6e7681; text-decoration: line-through; }}
table {{ width: 100%; border-collapse: collapse; font-size: .9em; }}
td {{ padding: 4px 6px; border-bottom: 1px solid #21262d; }}
tr:last-child td {{ border-bottom: none; }}
.pts {{ text-align: right; font-weight: 600; color: #ffa657; }}
ul {{ padding-left: 18px; }} li {{ margin: 6px 0; line-height: 1.45; }}
a {{ color: #a5d6ff; text-decoration: none; }} a:active {{ opacity: .7; }}
.ahead {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
.ahead span {{ font-size: .85em; color: #ffa657; white-space: nowrap; }}
.up {{ color: #3fb950; }} .down {{ color: #f85149; }} .flat {{ color: #8b949e; }}
.bar {{ height: 6px; background: #21262d; border-radius: 3px; margin: 8px 0 4px; }}
.bar i {{ display: block; height: 100%; border-radius: 3px;
  background: linear-gradient(90deg,#f778ba,#ffa657); }}
.sparkrow {{ margin: 2px 0 4px; }}
.spark {{ display: block; max-width: 100%; height: 30px; }}
footer {{ margin: 20px 0 8px; text-align: center; font-size: .75em; color: #484f58; }}
</style></head><body>
<h1>🛸 樾的热点仪表盘</h1>
<div class="dim small">更新于 {updated}(北京时间)· 每天 07:40 / 19:40 自动刷新 · by Lando</div>
<h2>🏎️ F1</h2>
{next_html}
<div class="card"><b class="small">车手积分榜</b><table>{drows}</table></div>
<div class="card"><b class="small">车队积分榜</b><table>{crows}</table></div>
<div class="card"><b class="small">最新消息</b><ul class="small">{nrows}</ul></div>
<h2>🎵 歌星热度榜</h2>
{acards}
<footer>信源:Autosport / Motorsport / F1.com / The Race / RaceFans / PlanetF1 / jolpica-f1
· Google News / Billboard / Rolling Stone / NME / Pitchfork<br>
热度 = 24h报道×2 + 48h报道 + 趋势加成(±10)· 曲线为近 14 次刷新走势</footer>
</body></html>"""


def main():
    now = datetime.now(timezone.utc)
    f1_news = collect_f1(now)
    standings = collect_standings(now)
    artists = collect_artists(now)
    prev_scores, hist = update_history(artists, now)
    out = BASE / "index.html"
    out.write_text(render(f1_news, standings, artists, prev_scores, hist, now), encoding="utf-8")
    print(f"OK -> {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
