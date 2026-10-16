"""Video Channel Health Monitor.

Pulls each channel's recent uploads and public statistics through the YouTube Data API v3, stores every pull
as a snapshot in SQLite, compares each recent upload with what the same channel's uploads of the same format
usually have at that age, and writes a static HTML report.

Usage (set the YOUTUBE_API_KEY environment variable first):
    python channel_health.py fetch     # pull one snapshot
    python channel_health.py report    # build report/index.html from the latest snapshot
    python channel_health.py report --run 1   # rebuild it for an earlier snapshot
    python channel_health.py all       # both
"""
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from html import escape

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "channel_health.db")
API = "https://www.googleapis.com/youtube/v3/"
COST = {"channels": 1, "playlistItems": 1, "videos": 1}  # units per call, from the default 10,000-a-day pool
# search.list has its own allowance (100 calls a day by default), so searches are counted separately.
MAX_VIDEOS = 200        # most recent uploads pulled per channel
HISTORY_DAYS = 365      # the baseline uses uploads up to a year old
KEEP_DAYS = 30          # YouTube's API Developer Policies cap stored API data at 30 days
RECENT_DAYS = 30        # only uploads from the last 30 days are flagged
MIN_AGE_DAYS = 1        # uploads younger than a day are too new to judge
MIN_GROUP = 8           # uploads a channel needs in one format before it gets a baseline
LOW, HIGH = 0.5, 2.0    # a flag needs under half or over double the expected views...
Z_LIMIT = 2.0           # ...and a gap more than 2 robust standard deviations from the channel's usual spread
MIN_SPREAD = 0.1        # floor on that spread (log scale), so a very steady channel can't divide by zero
SHORTS_3MIN_FROM = datetime(2024, 10, 15, tzinfo=timezone.utc)  # Shorts limit went from 60 seconds to 3 minutes
FORMATS = ("Videos", "Shorts", "Live and premieres")             # the content tabs in YouTube Studio
NOT_SCORED = "Scheduled or live now"


class QuotaExceeded(Exception):
    pass


class Client:
    """Thin API client: counts quota units, retries transient errors, stops cleanly on quota errors.
    Errors are logged without the request URL, so the key never reaches the log."""

    def __init__(self, key, session=None):
        self.key = key
        self.s = session or requests.Session()
        self.units = 0
        self.searches = 0
        self.calls = 0
        self.errors = []

    def get(self, resource, **params):
        params["key"] = self.key
        for attempt in range(4):
            try:
                r = self.s.get(API + resource, params=params, timeout=20)
            except requests.RequestException as e:
                self.errors.append(f"{resource}: network error ({type(e).__name__}), attempt {attempt + 1}")
                time.sleep(2 ** attempt)
                continue
            self.calls += 1
            if resource == "search":
                self.searches += 1
            else:
                self.units += COST[resource]
            if r.status_code == 200:
                return r.json()
            reason = ""
            try:
                reason = r.json()["error"]["errors"][0].get("reason", "")
            except Exception:
                pass
            if r.status_code == 403 and reason in ("quotaExceeded", "dailyLimitExceeded"):
                raise QuotaExceeded(reason)
            if r.status_code >= 500 or reason in ("rateLimitExceeded", "userRateLimitExceeded"):
                self.errors.append(f"{resource}: HTTP {r.status_code} {reason}, retrying")
                time.sleep(2 ** attempt)
                continue
            self.errors.append(f"{resource}: HTTP {r.status_code} {reason}")
            return None
        return None


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_duration(iso):
    """ISO 8601 duration (PT1H2M3S) to seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def name_matches(expected, title):
    """True when the channel title contains the expected name, ignoring case, accents and punctuation."""
    e, t = _norm(expected), _norm(title)
    return bool(e) and e in t


def parse_video(v):
    """One videos.list item to a flat row. Missing counts stay None, so a hidden view count isn't read as 0."""
    sn, cd, st, pl = v["snippet"], v.get("contentDetails", {}), v.get("statistics", {}), v.get("player", {})

    def num(d, k):
        return int(d[k]) if d.get(k) is not None else None

    return dict(video_id=v["id"], title=sn.get("title", ""), published=sn["publishedAt"],
                duration_s=parse_duration(cd.get("duration")),
                width=num(pl, "embedWidth"), height=num(pl, "embedHeight"),
                live_details=int("liveStreamingDetails" in v), broadcast=sn.get("liveBroadcastContent", "none"),
                views=num(st, "viewCount"), likes=num(st, "likeCount"), comments=num(st, "commentCount"))


def classify(r):
    """Videos, Shorts, or Live and premieres. The Data API has no Shorts field, so this applies YouTube's rule:
    a vertical or square upload up to 3 minutes long (60 seconds before 15 October 2024). The API marks live
    streams and premieres the same way, so they share a group."""
    if r["broadcast"] in ("upcoming", "live"):
        return NOT_SCORED
    if r["live_details"]:
        return "Live and premieres"
    limit = 180 if parse_ts(r["published"]) >= SHORTS_3MIN_FROM else 60
    if 0 < r["duration_s"] <= limit:
        if r["width"] and r["height"]:
            return "Shorts" if r["height"] >= r["width"] else "Videos"
        return "Shorts"  # no player size returned: judged by length alone, and the run log counts these
    return "Videos"


def theil_sen(xs, ys):
    """Line fit through the median of all pairwise slopes; a few breakout uploads can't drag it the way
    least squares would."""
    slopes = [(ys[j] - ys[i]) / (xs[j] - xs[i])
              for i in range(len(xs)) for j in range(i + 1, len(xs)) if abs(xs[j] - xs[i]) > 1e-9]
    if not slopes:
        return None
    b = statistics.median(slopes)
    a = statistics.median([y - b * x for x, y in zip(xs, ys)])
    return a, b


def score_videos(rows, now):
    """Adds age_days, expected views, ratio and flag to each row.

    For each channel and format with enough uploads aged 1 to 365 days, fits log(views) against log(age).
    Expected views is that line at the upload's age, so a 3-day-old upload is compared with what the channel's
    3-day-old uploads usually have. An upload from the last 30 days is flagged only when it is both far off in
    absolute terms (under half or over double) and unusual for that channel, whose views may normally swing
    widely (robust z-score beyond 2, using the median absolute deviation)."""
    out = [dict(r, age_days=max((now - parse_ts(r["published"])).total_seconds() / 86400, 0.0),
                expected=None, ratio=None, z=None, flag="") for r in rows]
    groups = {}
    for r in out:
        if r["fmt"] in FORMATS and r["views"] is not None and MIN_AGE_DAYS <= r["age_days"] <= HISTORY_DAYS:
            groups.setdefault((r["channel_id"], r["fmt"]), []).append(r)
    for g in groups.values():
        if len(g) < MIN_GROUP:
            continue
        fit = theil_sen([math.log(r["age_days"]) for r in g], [math.log1p(r["views"]) for r in g])
        if not fit:
            continue
        a, b = fit
        res = [math.log1p(r["views"]) - (a + b * math.log(r["age_days"])) for r in g]
        mid = statistics.median(res)
        spread = max(1.4826 * statistics.median([abs(x - mid) for x in res]), MIN_SPREAD)
        for r, e in zip(g, res):
            r["expected"] = math.expm1(math.log1p(r["views"]) - e)
            r["ratio"] = math.exp(e)
            r["z"] = (e - mid) / spread
            if r["age_days"] <= RECENT_DAYS:
                if r["ratio"] < LOW and r["z"] < -Z_LIMIT:
                    r["flag"] = "below expected"
                elif r["ratio"] > HIGH and r["z"] > Z_LIMIT:
                    r["flag"] = "above expected"
    return out


PARTS = "id,snippet,statistics,contentDetails"


def resolve_channel(c, entry):
    """Look a channel up by pinned ID (1 unit), then handle (1 unit), then search (1 call from the search allowance)."""
    if entry.get("id"):
        j = c.get("channels", part=PARTS, id=entry["id"])
        if j and j.get("items"):
            return j["items"][0], "id"
    if entry.get("handle"):
        j = c.get("channels", part=PARTS, forHandle=entry["handle"])
        if j and j.get("items"):
            return j["items"][0], "handle"
    j = c.get("search", part="snippet", q=entry["name"], type="channel", maxResults=1)
    if j and j.get("items"):
        j2 = c.get("channels", part=PARTS, id=j["items"][0]["snippet"]["channelId"])
        if j2 and j2.get("items"):
            return j2["items"][0], "search"
    return None, None


def recent_upload_ids(c, playlist_id, now):
    """Most recent uploads, newest first, until MAX_VIDEOS or uploads older than HISTORY_DAYS."""
    ids, token = [], None
    while len(ids) < MAX_VIDEOS:
        params = dict(part="contentDetails", playlistId=playlist_id, maxResults=50)
        if token:
            params["pageToken"] = token
        j = c.get("playlistItems", **params)
        if not j:
            break
        items = j.get("items", [])
        ids += [it["contentDetails"]["videoId"] for it in items]
        dates = [it["contentDetails"]["videoPublishedAt"] for it in items if it["contentDetails"].get("videoPublishedAt")]
        token = j.get("nextPageToken")
        if not token or not items or (dates and (now - parse_ts(min(dates))).days > HISTORY_DAYS):
            break
    return ids[:MAX_VIDEOS]


def db_connect():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS runs (run_id INTEGER PRIMARY KEY, ts TEXT, units INTEGER, calls INTEGER, log TEXT);
        CREATE TABLE IF NOT EXISTS channels (run_id INTEGER, channel_id TEXT, name TEXT, vertical TEXT, title TEXT,
            subs INTEGER, views INTEGER, video_count INTEGER);
        CREATE TABLE IF NOT EXISTS videos (run_id INTEGER, video_id TEXT, channel_id TEXT, title TEXT, published TEXT,
            duration_s INTEGER, width INTEGER, height INTEGER, live_details INTEGER, broadcast TEXT, fmt TEXT,
            views INTEGER, likes INTEGER, comments INTEGER);
    """)
    return con


VIDEO_COLS = ("video_id", "title", "published", "duration_s", "width", "height", "live_details", "broadcast",
              "fmt", "views", "likes", "comments")


def prune(con, now):
    """Deletes snapshots older than KEEP_DAYS, since YouTube's Developer Policies allow API data to be stored for
    30 days at most. Returns how many snapshots were deleted."""
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    old = [(r[0],) for r in con.execute("SELECT run_id FROM runs WHERE ts < ?", (cutoff,))]
    for table in ("videos", "channels", "runs"):
        con.executemany(f"DELETE FROM {table} WHERE run_id = ?", old)
    return len(old)


def fetch(key, channels, now=None):
    c = Client(key)
    now = now or datetime.now(timezone.utc)
    con = db_connect()
    pruned = prune(con, now)
    run_id = con.execute("INSERT INTO runs (ts, units, calls, log) VALUES (?, 0, 0, '[]')",
                         (now.isoformat(timespec="seconds"),)).lastrowid
    log = c.errors  # one list for API errors and data warnings
    if pruned:
        log.append(f"Deleted {pruned} snapshot{'s' if pruned != 1 else ''} older than {KEEP_DAYS} days")
    try:
        for entry in channels:
            ch, how = resolve_channel(c, entry)
            if not ch:
                log.append(f"{entry['name']}: channel not found")
                continue
            title = ch["snippet"]["title"]
            if not name_matches(entry["name"], title):
                if how != "id":
                    log.append(f"{entry['name']}: {how} lookup returned \"{title}\", skipped; pin the right channel ID")
                    continue
                log.append(f"{entry['name']}: pinned channel is now titled \"{title}\"")
            elif how != "id":
                log.append(f"{entry['name']}: found by {how}; pin ID {ch['id']} in channels.json")
            st = ch.get("statistics", {})
            con.execute("INSERT INTO channels VALUES (?,?,?,?,?,?,?,?)", (
                run_id, ch["id"], entry["name"], entry.get("vertical", ""), title,
                None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0)),
                int(st.get("viewCount", 0)), int(st.get("videoCount", 0))))
            ids = recent_upload_ids(c, ch["contentDetails"]["relatedPlaylists"]["uploads"], now)
            no_size = hidden = 0
            for i in range(0, len(ids), 50):
                vj = c.get("videos", part="snippet,contentDetails,statistics,liveStreamingDetails,player",
                           id=",".join(ids[i:i + 50]), maxHeight=1000, maxWidth=1000)
                for v in (vj or {}).get("items", []):
                    r = parse_video(v)
                    r["fmt"] = classify(r)
                    no_size += r["width"] is None and r["fmt"] == "Shorts"
                    hidden += r["views"] is None
                    vals = (run_id, r["video_id"], ch["id"], *[r[k] for k in VIDEO_COLS[1:]])
                    con.execute(f"INSERT INTO videos VALUES ({','.join('?' * len(vals))})", vals)
            if no_size:
                log.append(f"{entry['name']}: {no_size} upload{'s' if no_size != 1 else ''} had no player size, so Shorts were judged by length alone")
            if hidden:
                log.append(f"{entry['name']}: {hidden} upload{'s hide their' if hidden != 1 else ' hides its'} view count and "
                           f"{'were' if hidden != 1 else 'was'} left out of the scoring")
    except QuotaExceeded as e:
        log.append(f"stopped early: {e}")
    con.execute("UPDATE runs SET units=?, calls=?, log=? WHERE run_id=?", (c.units, c.calls, json.dumps(log), run_id))
    con.commit()
    con.close()
    return run_id, c


def growth(con, run):
    """Views gained by each upload present in both this snapshot and the one before it."""
    prev = con.execute("SELECT * FROM runs WHERE run_id < ? ORDER BY run_id DESC LIMIT 1", (run["run_id"],)).fetchone()
    if not prev:
        return None
    hours = (parse_ts(run["ts"]) - parse_ts(prev["ts"])).total_seconds() / 3600
    rows = [dict(r) for r in con.execute(
        """SELECT n.channel_id, n.video_id, n.title, n.fmt, n.views - p.views AS gained
           FROM videos n JOIN videos p ON p.video_id = n.video_id AND p.run_id = ?
           WHERE n.run_id = ? AND n.views IS NOT NULL AND p.views IS NOT NULL""", (prev["run_id"], run["run_id"]))]
    return dict(prev_ts=prev["ts"], hours=hours, rows=rows)


def report(run_id=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    con = db_connect()
    con.row_factory = sqlite3.Row
    run = (con.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone() if run_id else
           con.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone())
    if not run:
        sys.exit("No such snapshot yet. Run: python channel_health.py fetch")
    rid, now = run["run_id"], parse_ts(run["ts"])
    chans = [dict(r) for r in con.execute("SELECT * FROM channels WHERE run_id=? ORDER BY rowid", (rid,))]
    vids = score_videos([dict(r) for r in con.execute("SELECT * FROM videos WHERE run_id=?", (rid,))], now)
    grow = growth(con, run)
    nruns = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    con.close()
    by_ch = {c["channel_id"]: c for c in chans}

    summary = []
    for c in chans:
        vs = [v for v in vids if v["channel_id"] == c["channel_id"] and v["fmt"] in FORMATS
              and v["age_days"] <= HISTORY_DAYS]
        # days covered is the window the fetch checked: back to the 200th upload, or the full year if fewer
        span = min(max((v["age_days"] for v in vids if v["channel_id"] == c["channel_id"]), default=0), HISTORY_DAYS)
        summary.append(dict(c, n=len(vs), span=span, mix={f: sum(v["fmt"] == f for v in vs) for f in FORMATS},
                            per_week=len(vs) / (span / 7) if span >= 1 else None,
                            scored=sum(v["expected"] is not None for v in vs),
                            below=sum(v["flag"] == "below expected" for v in vs),
                            above=sum(v["flag"] == "above expected" for v in vs)))

    os.makedirs(os.path.join(HERE, "report"), exist_ok=True)
    colors = {"Videos": "#2F5D7C", "Shorts": "#D9822B", "Live and premieres": "#6B8E23"}
    fig, ax = plt.subplots(figsize=(8, 0.42 * len(summary) + 1.4))
    names = [s["title"][:28] for s in summary]
    ys = list(range(len(summary)))
    left = [0.0] * len(summary)
    for f in FORMATS:
        vals = [100 * s["mix"][f] / s["n"] if s["n"] else 0 for s in summary]
        ax.barh(ys, vals, left=left, color=colors[f], label=f)
        for i, (l, w) in enumerate(zip(left, vals)):
            if w >= 8:
                ax.text(l + w / 2, i, f"{w:.0f}%", ha="center", va="center", color="white", fontsize=8)
        left = [a + b for a, b in zip(left, vals)]
    ax.set_yticks(ys, names)
    for t in ax.get_yticklabels():
        t.set_parse_math(False)
    ax.set_xlim(0, 100)
    ax.set_xlabel("Share of uploads analysed (%)")
    ax.set_title("Format mix of each channel's uploads")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=3, frameon=False)
    ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "report", "format_mix.png"), dpi=160)
    plt.close(fig)

    def n0(x):
        return "n/a" if x is None else f"{x:,.0f}"

    def pct(x):
        return "n/a" if x is None else f"{100 * x:.1f}%"

    srows = "".join(
        f"<tr><td>{escape(s['title'])}</td><td>{escape(s['vertical'])}</td><td>{n0(s['subs'])}</td>"
        f"<td>{s['n']}</td><td>{s['span']:.0f}</td>"
        f"<td>{'n/a' if s['per_week'] is None else format(s['per_week'], ',.1f')}</td>"
        f"<td>{s['mix']['Videos']} / {s['mix']['Shorts']} / {s['mix']['Live and premieres']}</td>"
        f"<td>{s['below']}</td><td>{s['above']}</td></tr>" for s in summary)

    def vrow(v):
        return (f"<tr><td>{escape(by_ch[v['channel_id']]['title'])}</td>"
                f"<td><a href=\"https://www.youtube.com/watch?v={escape(v['video_id'])}\">{escape(v['title'][:80])}</a></td>"
                f"<td>{v['fmt']}</td><td>{v['age_days']:.1f}</td><td>{n0(v['views'])}</td><td>{n0(v['expected'])}</td>"
                f"<td>{v['ratio']:.2f}x</td></tr>")

    def vtable(rows, label):
        head = ("<tr><th>Channel</th><th>Upload</th><th>Format</th><th>Age (days)</th><th>Views</th>"
                "<th>Expected at this age</th><th>Ratio</th></tr>")
        if not rows:
            return f"<p class=\"note\">No uploads {label}.</p>"
        top = f"<table>{head}{''.join(vrow(v) for v in rows[:15])}</table>"
        if len(rows) > 15:
            top += (f"<details><summary>Show all {len(rows)}</summary><table>{head}"
                    f"{''.join(vrow(v) for v in rows[15:])}</table></details>")
        return top

    below = sorted([v for v in vids if v["flag"] == "below expected"], key=lambda v: v["ratio"])
    above = sorted([v for v in vids if v["flag"] == "above expected"], key=lambda v: -v["ratio"])

    if grow:
        per_ch = {}
        for g in grow["rows"]:
            per_ch[g["channel_id"]] = per_ch.get(g["channel_id"], 0) + g["gained"]
        top = sorted(grow["rows"], key=lambda g: -g["gained"])[:10]
        gsec = (f"<p class=\"note\">Compared with the snapshot at {escape(grow['prev_ts'])} UTC, "
                f"{grow['hours']:.1f} hours earlier, across {len(grow['rows'])} uploads present in both.</p>"
                "<table><tr><th>Channel</th><th>Views gained</th><th>Views gained per hour</th></tr>"
                + "".join(f"<tr><td>{escape(by_ch[cid]['title'])}</td><td>{n0(x)}</td><td>{n0(x / grow['hours'])}</td></tr>"
                          for cid, x in sorted(per_ch.items(), key=lambda kv: -kv[1]) if cid in by_ch)
                + "</table><table><tr><th>Channel</th><th>Upload</th><th>Format</th><th>Views gained</th></tr>"
                + "".join(f"<tr><td>{escape(by_ch[g['channel_id']]['title'])}</td><td>{escape(g['title'][:80])}</td>"
                          f"<td>{g['fmt']}</td><td>{n0(g['gained'])}</td></tr>" for g in top if g["channel_id"] in by_ch)
                + "</table>")
    else:
        gsec = "<p class=\"note\">This is the first snapshot. Run the monitor again later to see views gained between snapshots.</p>"

    log = json.loads(run["log"] or "[]")
    total = sum(s["n"] for s in summary)
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light">
<title>Video Channel Health Monitor</title>
<style>body{{font-family:Calibri,"Segoe UI",Arial,sans-serif;max-width:1040px;margin:24px auto;padding:0 16px;color:#1f2328;background:#fff;line-height:1.45}}
h1{{margin-bottom:4px}}h2{{margin-top:28px}}table{{border-collapse:collapse;width:100%;font-size:14px;margin:8px 0 16px}}
th,td{{border-bottom:1px solid #dde1e6;padding:6px 8px;text-align:left;vertical-align:top}}th{{background:#f2f4f7}}
td{{font-variant-numeric:tabular-nums}}.note{{color:#4b5563;font-size:13.5px;max-width:75ch}}img{{max-width:100%}}
.wrap{{overflow-x:auto}}summary{{cursor:pointer;margin:4px 0 12px}}</style></head><body>
<h1>Video Channel Health Monitor</h1>
<p class="note">Snapshot taken {now:%d %b %Y, %H:%M} UTC through the YouTube Data API v3: {len(chans)} channels and {total:,} uploads
from the last 12 months (up to {MAX_VIDEOS} most recent per channel). This pull used {run['units']} quota units in {run['calls']}
calls, against a default daily quota of 10,000. Snapshots stored so far: {nruns}.</p>
<h2>Channel summary</h2>
<div class="wrap"><table><tr><th>Channel</th><th>Vertical</th><th>Subscribers</th><th>Uploads analysed</th><th>Days covered</th>
<th>Uploads per week</th><th>Videos / Shorts / Live and premieres</th>
<th>Below expected (last 30 days)</th><th>Above expected (last 30 days)</th></tr>{srows}</table></div>
<p class="note">Days covered is the window the monitor checked: back to a channel's 200th most recent upload, or the full
year when it posted fewer.</p>
<img src="format_mix.png" alt="Share of each channel's uploads that are videos, Shorts, or live streams and premieres">
<h2>Recent uploads below expected</h2>
<p class="note">Uploads from the last {RECENT_DAYS} days with under {LOW}x the views the same channel's uploads of the same format
usually have at that age, where the gap is also unusual for that channel (robust z-score below -{Z_LIMIT:g}).</p>
<div class="wrap">{vtable(below, 'below expected')}</div>
<h2>Recent uploads above expected</h2>
<p class="note">Uploads from the last {RECENT_DAYS} days with over {HIGH}x the expected views and a robust z-score above {Z_LIMIT:g}.</p>
<div class="wrap">{vtable(above, 'above expected')}</div>
<h2>Growth since the previous snapshot</h2>
<div class="wrap">{gsec}</div>
<h2>Method</h2>
<p class="note">For each channel and format with at least {MIN_GROUP} uploads aged 1 to 365 days, the monitor fits a line through
log(views) against log(age) using the Theil-Sen method, which takes the median of all pairwise slopes, so a few breakout uploads
have little effect on the line. Expected views is that line at an upload's age, and the ratio is actual views over expected. Views on busy
sports and news channels often come in at a quarter or four times the expected figure, so a flag needs two things: a ratio under {LOW}x or over
{HIGH}x, and a gap of more than {Z_LIMIT:g} robust standard deviations (1.4826 times the median absolute deviation of that channel's
gaps, on the log scale). Shorts follow
YouTube's rule: vertical or square and up to 3 minutes long (60 seconds before 15 October 2024), with orientation read from the
player size the API returns. The API marks live streams and premieres the same way, so they're grouped together. Scheduled and
in-progress streams, and uploads that hide their view count, aren't scored.</p>
<h2>Run log</h2><p class="note">{'<br>'.join(escape(e) for e in log) or 'No errors or warnings.'}</p>
<h2>Terms</h2><p class="note">Data from YouTube API Services. By using this tool, you agree to be bound by the
<a href="https://www.youtube.com/t/terms">YouTube Terms of Service</a>. The tool collects no personal data and deletes stored
YouTube data after {KEEP_DAYS} days. <a href="https://www.google.com/policies/privacy">Google Privacy Policy</a>. Expected views and
flags are this project's own calculations, not YouTube data. Not affiliated with or endorsed by YouTube or Google.</p>
</body></html>"""
    with open(os.path.join(HERE, "report", "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    return summary, below, above, run


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd not in ("fetch", "report", "all"):
        sys.exit(__doc__)
    if cmd in ("fetch", "all"):
        key = os.environ.get("YOUTUBE_API_KEY")
        if not key:
            sys.exit("Set the YOUTUBE_API_KEY environment variable first.")
        with open(os.path.join(HERE, "channels.json"), encoding="utf-8") as fh:
            channels = json.load(fh)
        run_id, c = fetch(key, channels)
        print(f"snapshot {run_id}: {c.calls} calls, {c.units} quota units, {c.searches} searches")
        for e in c.errors:
            print("  -", e)
    if cmd in ("report", "all"):
        run_id = int(sys.argv[sys.argv.index("--run") + 1]) if "--run" in sys.argv else None
        summary, below, above, run = report(run_id)
        print(f"report: {len(summary)} channels, {sum(s['n'] for s in summary)} uploads, "
              f"{len(below)} below and {len(above)} above expected -> report/index.html")


if __name__ == "__main__":
    main()
