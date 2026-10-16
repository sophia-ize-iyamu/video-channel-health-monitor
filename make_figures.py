"""Video Channel Health Monitor: README charts.

Builds the PNG charts in docs/ from one snapshot in data/channel_health.db.

Usage:
    python make_figures.py            # the latest snapshot
    python make_figures.py --run 1    # snapshot 1, the one the README documents

The database is opened read-only. Uploads are scored with channel_health.score_videos and the per-channel
figures follow channel_health.report(), so each chart matches report/index.html for the same snapshot.
growth.png compares the chosen snapshot with the next one, or with the one before it when the chosen snapshot
is the latest. Every chart is 1440 px wide (9 in at 160 dpi) on a white background, with plain linear axes,
and ends with a footer line giving the snapshot time and the source. Each fetch deletes snapshots older than
channel_health.KEEP_DAYS days, so a chart can only be rebuilt while its snapshot is still stored.
"""
import argparse
import math
import os
import pathlib
import sqlite3
import sys
from datetime import timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager, transforms  # noqa: E402
from matplotlib.backends.backend_agg import RendererAgg  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402
from matplotlib.text import Text  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

from channel_health import (DB, FORMATS, HIGH, HISTORY_DAYS, KEEP_DAYS, LOW, MAX_VIDEOS,  # noqa: E402
                            MIN_AGE_DAYS, NOT_SCORED, RECENT_DAYS, Z_LIMIT, growth, parse_ts, score_videos)

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")

W, DPI = 9.0, 160        # figure width in inches and resolution: 1440 px wide
OUT = 0.32               # outer padding, inches
ROW = 0.4                # inches per row in the bar charts
GAP = 8                  # points between an axis or a bar end and its label
PART_GAP = 7             # points between two parts of one label, such as a channel and its vertical
PAD = 4                  # points of fill kept on each side of a label set inside a bar segment
SWATCH = 7               # points, the colour key beside a segment label set outside its bar
ARROW = 22               # points, length of the arrow over an upload that's off the top of the chart
MIN_PT = 9               # smallest text size allowed
MIN_CONTRAST = 4.5       # a label goes inside a bar segment only with this contrast against the fill (WCAG AA)
LABEL_PAD = 0.15         # white box around a label drawn over the grid, as a fraction of the text size
FOOT_PT = 9              # footer text size
FOOT_Y = 0.24            # inches from the bottom of the image to the bottom of the footer
SOURCE = "Source: YouTube Data API v3."
OWN_CALC = "Expected views and flags are this project's own calculations."
WHITE, INK, MUTED, LINE = "#FFFFFF", "#1F2328", "#57606A", "#D0D7DE"
FMT_COLORS = {"Videos": "#2F5D7C", "Shorts": "#D9822B", "Live and premieres": "#6B8E23"}
ABOVE, BELOW, NOT_FLAGGED = "#1F7A4D", "#B42318", "#8C959F"
MARKS = {"above expected": (ABOVE, "^"), "below expected": (BELOW, "v"), "": (NOT_FLAGGED, "o")}
FLAG_NAMES = {"": "Not flagged", "above expected": "Flagged above expected", "below expected": "Flagged below expected"}
BAR = MUTED              # bars for a single measure carry no format or flag colour

# Ratio bins for docs/ratio_distribution.png. Each bin includes its lower edge and excludes its upper edge
# (lo <= ratio < hi), so a ratio of exactly 0.5 counts in "0.5-1x", in line with the strict "under 0.5x" test.
BINS = ((0.0, 0.25, "Under 0.25x"), (0.25, 0.5, "0.25-0.5x"), (0.5, 1.0, "0.5-1x"), (1.0, 2.0, "1-2x"),
        (2.0, 4.0, "2-4x"), (4.0, 10.0, "4-10x"), (10.0, math.inf, "Over 10x"))

_INSTALLED = {f.name for f in font_manager.fontManager.ttflist}
FONT = next((f for f in ("Segoe UI", "Helvetica Neue", "Arial") if f in _INSTALLED), "DejaVu Sans")
plt.rcParams.update({
    "font.family": FONT, "font.size": 10, "text.color": INK, "text.parse_math": False,
    "axes.labelsize": 11, "axes.labelcolor": MUTED, "axes.edgecolor": LINE, "axes.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "axes.facecolor": WHITE, "axes.axisbelow": True,
    "xtick.labelsize": 10, "ytick.labelsize": 10, "xtick.color": MUTED, "ytick.color": MUTED,
    "xtick.major.size": 0, "ytick.major.size": 0, "xtick.major.pad": 6, "ytick.major.pad": 6,
    "grid.color": LINE, "grid.linewidth": 0.6, "legend.fontsize": 10,
    "figure.facecolor": WHITE, "savefig.facecolor": WHITE, "savefig.transparent": False,
})
_RENDERER = RendererAgg(10, 10, DPI)


# ---------------------------------------------------------------- data

def connect():
    if not os.path.exists(DB):
        sys.exit(f"No database at {DB}. Run: python channel_health.py fetch")
    con = sqlite3.connect(pathlib.Path(DB).as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load(con, run):
    """One snapshot, read and scored the way channel_health.report() does it."""
    chans = [dict(r) for r in con.execute("SELECT * FROM channels WHERE run_id=? ORDER BY rowid", (run["run_id"],))]
    vids = score_videos([dict(r) for r in con.execute("SELECT * FROM videos WHERE run_id=?", (run["run_id"],))],
                        parse_ts(run["ts"]))
    return chans, vids


def summarise(chans, vids):
    """Per-channel figures, computed as in channel_health.report()."""
    out = []
    for c in chans:
        mine = [v for v in vids if v["channel_id"] == c["channel_id"]]
        vs = [v for v in mine if v["fmt"] in FORMATS and v["age_days"] <= HISTORY_DAYS]
        # days covered: back to the oldest upload fetched for the channel, at most HISTORY_DAYS
        span = min(max((v["age_days"] for v in mine), default=0), HISTORY_DAYS)
        out.append(dict(c, n=len(vs), span=span, mix={f: sum(v["fmt"] == f for v in vs) for f in FORMATS},
                        per_week=len(vs) / (span / 7) if span >= 1 else None,
                        scored=sum(v["expected"] is not None for v in vs),
                        recent=sum(v["expected"] is not None and v["age_days"] <= RECENT_DAYS for v in vs),
                        below=sum(v["flag"] == "below expected" for v in vs),
                        above=sum(v["flag"] == "above expected" for v in vs)))
    return out


# ---------------------------------------------------------------- wording helpers

def utc_minute(ts):
    """A snapshot time in UTC, rounded to the nearest minute."""
    t = parse_ts(ts).astimezone(timezone.utc)
    return (t + timedelta(seconds=30)).replace(second=0, microsecond=0)


def day(t):
    return f"{t.day} {t:%b %Y}"


def clock(t):
    return f"{t:%H:%M} UTC"


def when(ts):
    t = utc_minute(ts)
    return f"{day(t)}, {clock(t)}"


def source_line(*stamps, own=False):
    """Footer for a chart: its snapshot time (or two times, for growth.png), the source and, on charts that show
    expected views or flags, a note that those are this project's own calculations."""
    t = [utc_minute(s) for s in stamps]
    if len(t) == 1:
        times = f"Snapshot {when(stamps[0])}."
    elif t[0].date() == t[1].date():
        times = f"Snapshots {day(t[0])}, {t[0]:%H:%M} and {clock(t[1])}."
    else:
        times = f"Snapshots {when(stamps[0])} and {when(stamps[1])}."
    return " ".join([times, SOURCE] + ([OWN_CALC] if own else []))


def duration(hours):
    if hours < 2:
        return plural(round(hours * 60), "minute")
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def window():
    return "Last 12 months" if HISTORY_DAYS == 365 else f"Last {HISTORY_DAYS} days"


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def ratio_text(r):
    return f"{r:,.0f}x" if r >= 10 else f"{r:.1f}x"


def compact(x, _pos=None):
    if abs(x) >= 1e6:
        return f"{x / 1e6:g}M"
    if abs(x) >= 1e3:
        return f"{x / 1e3:g}K"
    return f"{x:g}"


def whole_percents(counts):
    """Whole-number shares that add up to 100 (largest remainder), so the labels on one bar sum to 100%."""
    n = sum(counts)
    raw = [100 * c / n for c in counts]
    out = [math.floor(r) for r in raw]
    for k in sorted(range(len(raw)), key=lambda k: out[k] - raw[k])[:100 - sum(out)]:
        out[k] += 1
    return out


# ---------------------------------------------------------------- drawing helpers

def text_width(s, size=10, weight="normal"):
    """Width of a one-line string in points, in the font the charts use."""
    prop = font_manager.FontProperties(family=FONT, size=size, weight=weight)
    w, _, _ = _RENDERER.get_text_width_height_descent(s, prop, ismath=False)
    return w * 72 / DPI


def parts_width(parts):
    return sum(text_width(s) for s, _ in parts) + PART_GAP * (len(parts) - 1)


def line_height(size=10):
    """Height in points of one line of text. Matplotlib gives every line the extent of "lp", ascender to
    descender, and centres that on the anchor when va="center"."""
    prop = font_manager.FontProperties(family=FONT, size=size)
    _, h, _ = _RENDERER.get_text_width_height_descent("lp", prop, ismath=False)
    return h * 72 / DPI


def _lum(hex_color):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(int(hex_color[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG contrast ratio between two colours, from 1 to 21."""
    hi, lo = sorted((_lum(a) + 0.05, _lum(b) + 0.05), reverse=True)
    return hi / lo


def on_fill(fill):
    """White or ink, whichever contrasts more with the fill, for a label set inside a bar."""
    return WHITE if contrast(WHITE, fill) >= contrast(INK, fill) else INK


def white_box(ax, x, y, x0, y0, w, h):
    """White box drawn over the grid and under a label: w by h points, its lower left corner x0, y0 points from
    the data point (x, y). Keeps gridlines from running through the label's text."""
    at = ax.figure.dpi_scale_trans + transforms.ScaledTranslation(x, y, ax.transData)
    ax.add_artist(Rectangle((x0 / 72, y0 / 72), w / 72, h / 72, transform=at, facecolor=WHITE, edgecolor="none",
                            zorder=4.9, clip_on=False))


def frame(height, title, subtitle=None, legend=None, footer=None, left=OUT, right=OUT, bottom=0.72, gap=0.16,
          ncol=None, **legend_kw):
    """White figure with a left-aligned title, subtitle and legend row, and a muted footer line under the plot.
    The plot is placed by margins in inches, `gap` below the legend and `bottom` above the footer. The footer
    adds to the figure's height, so the plot keeps the size it would have without one, and the space the bottom
    margin leaves under the axis label separates the two."""
    if footer:
        extra = FOOT_Y + line_height(FOOT_PT) / 72
        height, bottom = height + extra, bottom + extra
    fig = plt.figure(figsize=(W, height), dpi=DPI)
    fig.patch.set_facecolor(WHITE)
    if footer:
        fig.text(OUT / W, FOOT_Y / height, footer, size=FOOT_PT, color=MUTED, va="bottom")
    y = height - 0.25
    fig.text(OUT / W, y / height, title, size=13, weight="semibold", color=INK, va="top")
    y -= 0.31
    if subtitle:
        fig.text(OUT / W, y / height, subtitle, size=10, color=MUTED, va="top")
        y -= 0.26
    if legend:
        kw = dict(handlelength=0.9, handleheight=0.9, handletextpad=0.5, columnspacing=1.6)
        kw.update(legend_kw)
        leg = fig.legend(handles=legend, loc="upper left", bbox_to_anchor=(OUT / W, y / height),
                         ncol=ncol or len(legend), frameon=False, borderpad=0, borderaxespad=0, labelcolor=INK, **kw)
        y = leg.get_window_extent(fig.canvas.get_renderer()).y0 / fig.dpi - 0.05
    ax = fig.add_axes([left / W, bottom / height, (W - left - right) / W, (y - gap - bottom) / height])
    ax.set_facecolor(WHITE)
    return fig, ax


def ax_size(ax):
    """Plot area width and height in points."""
    b = ax.get_position()
    w, h = ax.figure.get_size_inches()
    return b.width * w * 72, b.height * h * 72


def bar_rows(ax, n):
    ax.set_ylim(n - 0.5, -0.5)
    ax.set_yticks([])
    ax.xaxis.grid(True)


def row_label(ax, i, parts):
    """Right-aligned label left of row i, drawn as [(text, colour), ...] on one line."""
    x = -GAP
    for s, color in reversed(parts):
        ax.annotate(s, xy=(0, i), xycoords=("axes fraction", "data"), xytext=(x, 0), textcoords="offset points",
                    ha="right", va="center", color=color, annotation_clip=False)
        x -= text_width(s) + PART_GAP


def end_label(ax, x, i, parts, leftwards=False):
    """Label [(text, colour), ...] just past a bar end at x, running away from the bar. It's drawn over the grid
    on one white box that spans all its parts, so no gridline shows through the text or between the parts."""
    width = parts_width(parts)
    pos = -GAP - width if leftwards else GAP
    pad, h = LABEL_PAD * 10, line_height()
    white_box(ax, x, i, pos - pad, -h / 2 - pad, width + 2 * pad, h + 2 * pad)
    for s, color in parts:
        ax.annotate(s, xy=(x, i), xytext=(pos, 0), textcoords="offset points", ha="left", va="center",
                    color=color, zorder=5, annotation_clip=False)
        pos += text_width(s) + PART_GAP


def xlabel_under(ax, label, x_from, x_to):
    """x-axis label centred under the ticked part of the axis, when the axis runs on to make room for labels. It
    keeps the usual spacing: tick pad, a line of tick labels, then labelpad=8."""
    ax.set_xlabel(label)
    lo, hi = ax.get_xlim()
    ax.xaxis.set_label_coords(((x_from + x_to) / 2 - lo) / (hi - lo), -(6 + line_height() + 8) / ax_size(ax)[1])


def nice_ticks(vmax, integer=False):
    """Ticks from 0 up to the first one at or past vmax."""
    loc = MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10], integer=integer)
    out = []
    for t in loc.tick_values(0, vmax if vmax > 0 else 1):
        if t >= 0:
            out.append(t)
            if t >= vmax:
                break
    return out


def drawn_texts(fig):
    """Every piece of text that will be drawn, tick labels outside the view left out."""
    ticks, out = set(), []
    for ax in fig.axes:
        for axis, lim in ((ax.xaxis, ax.get_xlim()), (ax.yaxis, ax.get_ylim())):
            lo, hi = sorted(lim)
            for tick in axis.get_major_ticks() + axis.get_minor_ticks():
                for lab in (tick.label1, tick.label2):
                    ticks.add(id(lab))
                    if lab.get_visible() and lab.get_text().strip() and lo - 1e-9 <= tick.get_loc() <= hi + 1e-9:
                        out.append(lab)
    out += [t for t in fig.findobj(Text) if id(t) not in ticks and t.get_visible() and t.get_text().strip()]
    return out


def check_text(fig, name):
    """Prints a warning for text that's too small, runs off the image or overlaps other text."""
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    texts = drawn_texts(fig)
    for t in texts:
        if t.get_fontsize() < MIN_PT:
            print(f"  WARNING {name}: text under {MIN_PT} pt: {t.get_text()!r} ({t.get_fontsize():g} pt)")
    boxes = [(t.get_text(), t.get_window_extent(r)) for t in texts]
    fb = fig.bbox
    for s, b in boxes:
        if b.x0 < fb.x0 or b.x1 > fb.x1 or b.y0 < fb.y0 or b.y1 > fb.y1:
            print(f"  WARNING {name}: text runs off the image: {s!r}")
    for i, (s1, b1) in enumerate(boxes):
        for s2, b2 in boxes[i + 1:]:
            if min(b1.x1, b2.x1) - max(b1.x0, b2.x0) > 1 and min(b1.y1, b2.y1) - max(b1.y0, b2.y0) > 1:
                print(f"  WARNING {name}: text overlaps: {s1!r} and {s2!r}")


def save(fig, name):
    check_text(fig, name)
    path = os.path.join(DOCS, name)
    fig.savefig(path, dpi=DPI, facecolor=WHITE)
    plt.close(fig)
    print(f"  wrote {os.path.relpath(path, HERE)}")


# ---------------------------------------------------------------- 1. format mix

def fig_format_mix(summary, vids, run):
    rows = sorted([s for s in summary if s["n"]], key=lambda s: -s["mix"]["Shorts"] / s["n"])
    pcts = [dict(zip(FORMATS, whole_percents([s["mix"][f] for f in FORMATS]))) for s in rows]

    def seg_text(p, f):
        return f"{p[f]}%" if p[f] else "<1%"

    labels = [[(s["title"], INK), (s["vertical"], MUTED)] for s in rows]
    head = "Uploads analysed"
    col = max([text_width(head)] + [text_width(f"{s['n']:,}") for s in rows])
    left = OUT + (max(map(parts_width, labels)) + GAP) / 72

    def right_pts(outw):  # bar end, labels set outside the bar, then the uploads column
        return GAP + (outw + 2 * GAP if outw else 0) + col

    # A segment's label goes inside it when it fits with PAD to spare and its text has MIN_CONTRAST against the
    # fill; otherwise it goes past the bar end with a colour key. White or ink on the olive Live and premieres fill
    # gives 4.2:1 at best, so those labels always go outside. Outside labels take width from the plot, so settle
    # the layout before drawing.
    legible = {f: contrast(on_fill(FMT_COLORS[f]), FMT_COLORS[f]) >= MIN_CONTRAST for f in FORMATS}
    outw, outside = 0.0, [[] for _ in rows]
    for _ in range(5):
        plot_w = (W - left - OUT) * 72 - right_pts(outw)
        outside = [[f for f in FORMATS if s["mix"][f] and (not legible[f] or
                    text_width(seg_text(p, f)) + 2 * PAD > s["mix"][f] / s["n"] * plot_w)]
                   for s, p in zip(rows, pcts)]
        need = max([sum(SWATCH + 3 + text_width(seg_text(p, f)) for f in o) + PART_GAP * (len(o) - 1)
                    for o, p in zip(outside, pcts) if o] or [0.0])
        if abs(need - outw) < 0.01:
            break
        outw = need

    fig, ax = frame(1.22 + ROW * len(rows) + 0.72, "Format mix by channel",
                    f"{window()}, up to {MAX_VIDEOS} uploads per channel",
                    legend=[Patch(color=FMT_COLORS[f], label=f) for f in FORMATS], footer=source_line(run["ts"]),
                    left=left, right=OUT + right_pts(outw) / 72)
    bar_rows(ax, len(rows))
    ax.spines["left"].set_visible(False)
    for i, (s, p, parts, out) in enumerate(zip(rows, pcts, labels, outside)):
        row_label(ax, i, parts)
        x = 0.0
        for f in FORMATS:
            share = 100 * s["mix"][f] / s["n"]
            if share > 0:
                ax.barh(i, share, left=x, height=0.62, color=FMT_COLORS[f], edgecolor=WHITE, linewidth=1, zorder=3)
                if f not in out:
                    ax.text(x + share / 2, i, seg_text(p, f), ha="center", va="center",
                            color=on_fill(FMT_COLORS[f]), zorder=4)
            x += share
        pos = GAP
        for f in out:
            key = transforms.offset_copy(ax.get_yaxis_transform(), fig=fig, x=pos + SWATCH / 2, y=0, units="points")
            ax.plot([1], [i], marker="s", markersize=SWATCH, markeredgewidth=0, color=FMT_COLORS[f], transform=key,
                    clip_on=False, zorder=4)
            ax.annotate(seg_text(p, f), xy=(1, i), xycoords=("axes fraction", "data"), xytext=(pos + SWATCH + 3, 0),
                        textcoords="offset points", ha="left", va="center", color=INK, annotation_clip=False)
            pos += SWATCH + 3 + text_width(seg_text(p, f)) + PART_GAP
        ax.annotate(f"{s['n']:,}", xy=(1, i), xycoords=("axes fraction", "data"), xytext=(right_pts(outw), 0),
                    textcoords="offset points", ha="right", va="center", color=INK, annotation_clip=False)
    ax.annotate(head, xy=(1, 1), xycoords="axes fraction", xytext=(right_pts(outw), 5), textcoords="offset points",
                ha="right", va="bottom", color=MUTED, annotation_clip=False)
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.0f}%"))
    ax.set_xlabel("Share of uploads analysed", labelpad=8)
    save(fig, "format_mix.png")

    total = sum(s["n"] for s in summary)
    by_fmt = {f: sum(s["mix"][f] for s in summary) for f in FORMATS}
    print(f"  {total:,} uploads analysed: " + ", ".join(f"{f} {by_fmt[f]:,} ({100 * by_fmt[f] / total:.1f}%)"
                                                       for f in FORMATS)
          + f"; {sum(v['fmt'] == NOT_SCORED for v in vids)} '{NOT_SCORED}' uploads left out")
    for s, p, out in zip(rows, pcts, outside):
        shares = ", ".join(f"{f} {s['mix'][f]} ({100 * s['mix'][f] / s['n']:.1f}%, shown {seg_text(p, f)}"
                           f"{' outside' if f in out else ''})" for f in FORMATS if s["mix"][f])
        print(f"  {s['title']} ({s['vertical']}): n={s['n']}; {shares}")


# ---------------------------------------------------------------- 2. cadence

def fig_cadence(summary, run):
    rows = sorted(summary, key=lambda s: (s["per_week"] is None, -(s["per_week"] or 0)))

    def parts(s):
        pw = s["per_week"]
        if pw is None:
            return [("n/a", MUTED), (plural(s["n"], "upload") + " analysed", MUTED)]
        days = f"{s['span']:.1f}" if s["span"] < 20 else f"{s['span']:.0f}"
        rate = f"{pw:,.0f} a week" if pw >= 10 else f"{pw:.1f} a week"
        return [(rate, INK), (f"{plural(s['n'], 'upload')} over {days} days", MUTED)]

    fig, ax = frame(0.98 + ROW * len(rows) + 0.72, "Uploads per week by channel",
                    f"{window()}, up to {MAX_VIDEOS} uploads per channel", footer=source_line(run["ts"]),
                    left=OUT + (max(text_width(s["title"]) for s in rows) + GAP) / 72)
    bar_rows(ax, len(rows))
    vals = [s["per_week"] or 0 for s in rows]
    ticks = nice_ticks(max(vals))
    plot_w, _ = ax_size(ax)
    xmax = ticks[-1]
    for v, s in zip(vals, rows):
        xmax = max(xmax, v * plot_w / (plot_w - GAP - parts_width(parts(s)) - 2))
    for i, (v, s) in enumerate(zip(vals, rows)):
        row_label(ax, i, [(s["title"], INK)])
        ax.barh(i, v, height=0.62, color=BAR, zorder=3)
        end_label(ax, v, i, parts(s))
    ax.set_xlim(0, xmax)
    ax.set_xticks(ticks)
    ax.spines["bottom"].set_bounds(0, ticks[-1])
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:g}"))
    xlabel_under(ax, "Uploads per week", 0, ticks[-1])
    save(fig, "cadence.png")
    for s in rows:
        pw = "n/a" if s["per_week"] is None else f"{s['per_week']:.3f}"
        print(f"  {s['title']}: {pw} a week, {s['n']} uploads over {s['span']:.2f} days")


# ---------------------------------------------------------------- 3. ratio distribution

def fig_ratio(vids, run):
    recent = [v for v in vids if v["expected"] is not None and v["age_days"] <= RECENT_DAYS]
    if not recent:
        print(f"  ratio_distribution.png skipped: no uploads from the last {RECENT_DAYS} days have a baseline")
        return
    bins = []
    for lo, hi, name in BINS:
        b = [v for v in recent if lo <= v["ratio"] < hi]
        below = sum(v["flag"] == "below expected" for v in b)
        above = sum(v["flag"] == "above expected" for v in b)
        bins.append(dict(name=name, n=len(b), below=below, above=above, plain=len(b) - below - above))
    under, over = sum(v["ratio"] < LOW for v in recent), sum(v["ratio"] > HIGH for v in recent)
    fixed = under + over
    n_below = sum(v["flag"] == "below expected" for v in recent)
    n_above = sum(v["flag"] == "above expected" for v in recent)

    fig, ax = frame(5.3, "Actual views as a multiple of expected views",
                    f"{len(recent):,} uploads aged {MIN_AGE_DAYS} to {RECENT_DAYS} days with a baseline",
                    legend=[Patch(color=BELOW, label="Flagged below expected"),
                            Patch(color=ABOVE, label="Flagged above expected"),
                            Patch(color=NOT_FLAGGED, label="Not flagged")],
                    footer=source_line(run["ts"], own=True), left=OUT + 0.62)
    plot_w, plot_h = ax_size(ax)

    # Comparison table, top right of the plot
    head = "Uploads flagged"
    table = [(f"Cut-off alone, under {LOW:g}x or over {HIGH:g}x", fixed),
             ("Cut-off and spread check", n_below + n_above)]
    num_w = max(text_width(f"{v:,}") for _, v in table)
    name_w = max(text_width(s) for s, _ in table + [(head, 0)])
    x0 = -(num_w + 16 + name_w)
    table_h = 16 * len(table) + 14

    # y limit: room for each column's labels, and for the table above the columns it spans
    slot = plot_w / len(bins)
    top = max(b["n"] for b in bins)
    for i, b in enumerate(bins):
        lines = 2 if b["below"] + b["above"] else 1
        need = 6 + 15 * lines
        half = max(text_width(f"{b['below'] + b['above']} flagged"), text_width(f"{b['n']:,}")) / 2
        if (i + 0.5) * slot + half > plot_w + x0 - 6:
            need += table_h + 6
        top = max(top, b["n"] / (1 - need / plot_h))
    ax.set_ylim(0, top)
    ax.set_xlim(-0.5, len(bins) - 0.5)

    pad, line_h = LABEL_PAD * 10, line_height()
    for i, b in enumerate(bins):
        base = 0
        for key, color in (("below", BELOW), ("above", ABOVE), ("plain", NOT_FLAGGED)):
            if b[key]:
                ax.bar(i, b[key], width=0.7, bottom=base, color=color, edgecolor=WHITE, linewidth=0.6, zorder=3)
                base += b[key]
        # the count, and the flags above it, on one white box over the grid
        labels = [(f"{b['n']:,}", INK)] + ([(f"{b['below'] + b['above']} flagged", MUTED)]
                                            if b["below"] + b["above"] else [])
        w = max(text_width(s) for s, _ in labels)
        white_box(ax, i, b["n"], -w / 2 - pad, 4 - pad, w + 2 * pad, 15 * (len(labels) - 1) + line_h + 2 * pad)
        for k, (s, color) in enumerate(labels):
            ax.annotate(s, xy=(i, b["n"]), xytext=(0, 4 + 15 * k), textcoords="offset points", ha="center",
                        va="bottom", color=color, zorder=5)

    ax.annotate(head, xy=(1, 1), xycoords="axes fraction", xytext=(x0, 0), textcoords="offset points",
                ha="left", va="top", color=MUTED)
    for j, (s, v) in enumerate(table, start=1):
        ax.annotate(s, xy=(1, 1), xycoords="axes fraction", xytext=(x0, -16 * j), textcoords="offset points",
                    ha="left", va="top", color=INK)
        ax.annotate(f"{v:,}", xy=(1, 1), xycoords="axes fraction", xytext=(0, -16 * j), textcoords="offset points",
                    ha="right", va="top", color=INK, weight="semibold")

    ppu = plot_h / top
    ax.set_yticks([t for t in MaxNLocator(nbins=6, integer=True).tick_values(0, top)
                   if 0 <= t <= top - (table_h + 8) / ppu])
    ax.yaxis.grid(True)
    ax.spines["left"].set_visible(False)
    ax.set_xticks(range(len(bins)))
    ax.set_xticklabels([b["name"] for b in bins])
    ax.set_xlabel("Multiple of expected views", labelpad=8)
    ax.set_ylabel("Uploads", labelpad=8)
    save(fig, "ratio_distribution.png")

    print(f"  {len(recent):,} uploads with a baseline, last {RECENT_DAYS} days")
    for b in bins:
        print(f"  {b['name']}: {b['n']} (flagged below {b['below']}, above {b['above']}, not flagged {b['plain']})")
    print(f"  cut-off alone: {fixed} of {len(recent)} ({100 * fixed / len(recent):.1f}%; {under} under {LOW:g}x, "
          f"{over} over {HIGH:g}x); cut-off and spread check (robust z beyond {Z_LIMIT:g}): {n_below + n_above} "
          f"({n_above} above, {n_below} below)")


# ---------------------------------------------------------------- 4. flags by channel

def fig_flags(summary, run):
    rows = sorted(summary, key=lambda s: (-(s["above"] + s["below"]), -s["recent"]))
    above, below, recent = (sum(s[k] for s in rows) for k in ("above", "below", "recent"))
    head = "Recent uploads\nwith a baseline"
    col = max([text_width(h) for h in head.split("\n")] + [text_width(f"{s['recent']:,}") for s in rows])
    fig, ax = frame(1.22 + ROW * len(rows) + 0.72, f"Uploads flagged above or below expected, last {RECENT_DAYS} days",
                    f"{above} above and {below} below, out of {recent:,} recent uploads with a baseline",
                    legend=[Patch(color=BELOW, label="Flagged below expected"),
                            Patch(color=ABOVE, label="Flagged above expected")],
                    footer=source_line(run["ts"], own=True),
                    left=OUT + (max(text_width(s["title"]) for s in rows) + GAP) / 72, right=OUT + (GAP + col) / 72,
                    gap=0.3)
    bar_rows(ax, len(rows))
    plot_w, _ = ax_size(ax)
    b_max, a_max = max(s["below"] for s in rows), max(s["above"] for s in rows)
    wl = max(text_width(str(s["below"])) for s in rows) + GAP + 3
    wr = max(text_width(str(s["above"])) for s in rows) + GAP + 3
    lft, rgt = 1.0, 1.0
    for _ in range(12):  # extents in data units that leave room for the end labels on both sides
        u = plot_w / (lft + rgt)
        lft = max(b_max + wl / u, 0.2 * (lft + rgt))
        rgt = a_max + wr / u
    ax.set_xlim(-lft, rgt)
    ax.set_xticks([t for t in MaxNLocator(nbins=8, integer=True, steps=[1, 2, 5, 10]).tick_values(-lft, rgt)
                   if -lft <= t <= rgt])
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{abs(x):g}"))
    ax.spines["left"].set_visible(False)
    ax.axvline(0, color=MUTED, linewidth=0.9, zorder=4)
    for i, s in enumerate(rows):
        row_label(ax, i, [(s["title"], INK)])
        if s["below"]:
            ax.barh(i, -s["below"], height=0.62, color=BELOW, zorder=3)
        if s["above"]:
            ax.barh(i, s["above"], height=0.62, color=ABOVE, zorder=3)
        end_label(ax, -s["below"], i, [(str(s["below"]), INK if s["below"] else MUTED)], leftwards=True)
        end_label(ax, s["above"], i, [(str(s["above"]), INK if s["above"] else MUTED)])
        ax.annotate(f"{s['recent']:,}", xy=(1, i), xycoords=("axes fraction", "data"), xytext=(GAP + col, 0),
                    textcoords="offset points", ha="right", va="center", color=MUTED, annotation_clip=False)
    ax.annotate(head, xy=(1, 1), xycoords="axes fraction", xytext=(GAP + col, 5), textcoords="offset points",
                ha="right", va="bottom", color=MUTED, multialignment="right", annotation_clip=False)
    ax.set_xlabel("Uploads flagged", labelpad=8)
    save(fig, "flags_by_channel.png")
    print(f"  total: {above} above, {below} below, {recent} recent uploads with a baseline")
    for s in rows:
        print(f"  {s['title']}: {s['above']} above, {s['below']} below, {s['recent']} with a baseline")


# ---------------------------------------------------------------- 5. expected-views curve for one group

def group_line(g, name):
    """The fitted line and spread for one channel and format, read back from score_videos' output.

    Within a group, log1p(expected) is a straight line in log(age), and each upload's z-score is
    (log(ratio) - mid) / spread. Two uploads fix each relation and the rest confirm it, so the chart draws
    exactly what score_videos used without refitting anything."""
    old, new = min(g, key=lambda v: v["age_days"]), max(g, key=lambda v: v["age_days"])
    b = ((math.log1p(new["expected"]) - math.log1p(old["expected"]))
         / (math.log(new["age_days"]) - math.log(old["age_days"])))
    a = math.log1p(old["expected"]) - b * math.log(old["age_days"])
    lo_z, hi_z = min(g, key=lambda v: v["z"]), max(g, key=lambda v: v["z"])
    if hi_z["z"] - lo_z["z"] < 1e-9:
        return a, b, None, None
    spread = (math.log(hi_z["ratio"]) - math.log(lo_z["ratio"])) / (hi_z["z"] - lo_z["z"])
    mid = math.log(lo_z["ratio"]) - lo_z["z"] * spread
    worst = max(max(abs(math.log1p(v["expected"]) - a - b * math.log(v["age_days"])),
                    abs(math.log(v["ratio"]) - mid - v["z"] * spread)) for v in g)
    if worst > 1e-6:
        sys.exit(f"The {name} line read back from score_videos doesn't fit its uploads (gap {worst:.2e}).")
    return a, b, mid, spread


def fig_expected_curve(chans, vids, run, channel="TSN", fmt="Shorts"):
    c = next((c for c in chans if c["title"] == channel), None)
    g = [v for v in vids if c and v["channel_id"] == c["channel_id"] and v["fmt"] == fmt and v["expected"] is not None]
    if not g:
        print(f"  expected_curve_example.png skipped: no {channel} {fmt} with a baseline in this snapshot")
        return
    a, b, mid, spread = group_line(g, f"{channel} {fmt}")

    def at(k, age):
        """Views at k times expected for this age, on score_videos' scale (ratio = (1 + views) / (1 + expected))."""
        return k * math.exp(a + b * math.log(age)) - 1

    # A recent upload is flagged above expected when its ratio clears both HIGH and the channel's spread check.
    up = max(HIGH, math.exp(mid + Z_LIMIT * spread)) if spread else None
    lo, hi = min(v["age_days"] for v in g), max(v["age_days"] for v in g)
    xs = [lo + (hi - lo) * i / 299 for i in range(300)]
    flag_xs = [x for x in xs if x <= RECENT_DAYS] if up else []
    unflagged = max((v["views"] for v in g if not v["flag"]), default=0)
    top = 1.12 * max([unflagged] + [at(HIGH, x) for x in xs] + [at(up, x) for x in flag_xs])
    clipped = sorted([v for v in g if v["views"] > top], key=lambda v: v["age_days"])
    present = [f for f in ("", "above expected", "below expected") if any(v["flag"] == f for v in g)]
    legend = [Line2D([], [], color=INK, linewidth=2, label="Expected views"),
              Line2D([], [], color=MUTED, linewidth=1.4, linestyle=(0, (4, 3)),
                     label=f"{HIGH:g}x and {LOW:g}x expected")]
    legend += [Line2D([], [], linestyle="none", marker=MARKS[f][1], markersize=7, markerfacecolor=MARKS[f][0],
                      markeredgecolor=WHITE, label=FLAG_NAMES[f]) for f in present]

    # Labels for uploads off the top of the chart go beside their arrows, above the plot: place them first,
    # so the gap above the plot fits however many rows they need.
    left, xmax = OUT + 0.62, math.ceil(hi) + 0.5
    plot_w = (W - left - OUT) * 72
    spots = []
    for idx, v in enumerate(clipped):
        label = f"{v['views']:,} views, {ratio_text(v['ratio'])} expected"
        w, px = text_width(label), v["age_days"] / xmax * plot_w
        arrows = [u["age_days"] / xmax * plot_w for u in clipped if u is not v]
        sides = ("left", "right") if len(clipped) > 1 and idx < len(clipped) / 2 else ("right", "left")
        spot = None
        for row in range(len(clipped)):
            for side in sides:
                x0, x1 = (px + 8, px + 8 + w) if side == "right" else (px - 8 - w, px - 8)
                if (x0 >= -(left - OUT) * 72 and x1 <= plot_w
                        and all(x1 + 6 < p0 or x0 - 6 > p1 for p0, p1, r, _ in spots if r == row)
                        and all(not (x0 - 4 <= q <= x1 + 4) for q in arrows)):
                    spot = (x0, x1, row, side)
                    break
            if spot:
                break
        spots.append(spot or (px + 8, px + 8 + w, len(clipped), "right"))
    rows_used = 1 + max((s[2] for s in spots), default=-1)
    above_plot = max(ARROW + 4, 12 + 16 * (rows_used - 1) + 8) + 8 if clipped else 6

    fig, ax = frame(6.4, f"Expected views by age: {channel} {fmt}",
                    f"{len(g):,} {fmt} with a baseline, aged {lo:.0f} to {hi:.0f} days",
                    legend=legend, footer=source_line(run["ts"], own=True), left=left, gap=above_plot / 72,
                    handlelength=2.2, handleheight=0.9)
    ax.plot(xs, [at(1, x) for x in xs], color=INK, linewidth=2, solid_capstyle="round", zorder=3)
    for k in (HIGH, LOW):
        ax.plot(xs, [at(k, x) for x in xs], color=MUTED, linewidth=1.4, linestyle=(0, (4, 3)), zorder=3)
    if flag_xs:
        ax.plot(flag_xs, [at(up, x) for x in flag_xs], color=ABOVE, linewidth=1.6, zorder=3)
        ax.annotate(f"Flag line: {up:.1f}x expected", xy=(flag_xs[-1], at(up, flag_xs[-1])), xytext=(0, 6),
                    textcoords="offset points", ha="right", va="bottom", color=INK)
    for f in present:
        color, marker = MARKS[f]
        pts = [v for v in g if v["flag"] == f and v["views"] <= top]
        ax.scatter([v["age_days"] for v in pts], [v["views"] for v in pts], s=44, marker=marker, color=color,
                   edgecolors=WHITE, linewidths=1, zorder=4)

    ax.set_xlim(0, xmax)
    ax.set_ylim(0, top)
    if hi > RECENT_DAYS:
        ax.axvline(RECENT_DAYS, color=LINE, linewidth=1, zorder=2)
        ax.annotate(f"{RECENT_DAYS}-day flag window", xy=(RECENT_DAYS, 0), xytext=(4, 4), textcoords="offset points",
                    ha="left", va="bottom", color=MUTED)

    # Uploads off the top of the chart: a marker on the top edge, an arrow up and their views and ratio.
    for v, (x0, x1, row, side) in zip(clipped, spots):
        color, marker = MARKS[v["flag"]]
        ax.scatter([v["age_days"]], [top], s=54, marker=marker, color=color, edgecolors=WHITE, linewidths=1,
                   zorder=5, clip_on=False)
        tip = transforms.offset_copy(ax.transData, fig=fig, x=0, y=ARROW, units="points")
        ax.annotate("", xy=(v["age_days"], top), xycoords=tip, xytext=(v["age_days"], top), textcoords="data",
                    arrowprops=dict(arrowstyle="-|>", color=color, linewidth=1.3, shrinkA=7, shrinkB=0,
                                    mutation_scale=10), annotation_clip=False)
        ax.annotate(f"{v['views']:,} views, {ratio_text(v['ratio'])} expected", xy=(v["age_days"], top),
                    xytext=(8 if side == "right" else -8, 12 + 16 * row), textcoords="offset points",
                    ha="left" if side == "right" else "right", va="center", color=INK, annotation_clip=False)

    ax.xaxis.set_major_locator(MaxNLocator(nbins=8, integer=True))
    ax.yaxis.set_major_formatter(FuncFormatter(compact))
    ax.yaxis.grid(True)
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("Age (days)", labelpad=8)
    ax.set_ylabel("Views", labelpad=8)
    save(fig, "expected_curve_example.png")

    print(f"  {channel} {fmt}: {len(g)} with a baseline, aged {lo:.2f} to {hi:.2f} days; "
          f"line a={a:.4f}, b={b:.4f}; mid={mid}, spread={spread}")
    print(f"  expected views {at(1, lo):,.0f} at {lo:.1f} days to {at(1, hi):,.0f} at {hi:.1f} days")
    if up:
        down = min(LOW, math.exp(mid - Z_LIMIT * spread))
        print(f"  flag line {up:.2f}x expected ({at(up, lo):,.0f} to {at(up, hi):,.0f} views); "
              f"below-expected line {down:.3f}x ({at(down, lo):,.0f} to {at(down, hi):,.0f} views)")
    print(f"  over {HIGH:g}x: {sum(v['ratio'] > HIGH for v in g)}, under {LOW:g}x: {sum(v['ratio'] < LOW for v in g)}; "
          f"flags: {', '.join(f'{FLAG_NAMES[f]} {sum(v['flag'] == f for v in g)}' for f in present)}")
    print(f"  largest unflagged {unflagged:,}; y-axis top {top:,.0f}")
    for v in clipped:
        print(f"  off the chart: {v['views']:,} views at {v['age_days']:.2f} days, expected {v['expected']:,.0f}, "
              f"{v['ratio']:.2f}x ({v['flag']})")


# ---------------------------------------------------------------- 6. growth between snapshots

def fig_growth(con, run, chans):
    later = con.execute("SELECT * FROM runs WHERE run_id > ? ORDER BY run_id LIMIT 1", (run["run_id"],)).fetchone()
    pair = later or run   # growth() compares a snapshot with the one before it
    g = growth(con, pair)
    if not g:
        print(f"  growth.png skipped: snapshot {run['run_id']} is the only snapshot, so there's nothing to compare")
        return
    if g["hours"] <= 0:
        print("  growth.png skipped: the two snapshots have the same timestamp")
        return
    if not g["rows"]:
        print("  growth.png skipped: no upload has a view count in both snapshots")
        return
    other = later["run_id"] if later else con.execute(
        "SELECT run_id FROM runs WHERE run_id < ? ORDER BY run_id DESC LIMIT 1", (run["run_id"],)).fetchone()[0]
    titles = {c["channel_id"]: c["title"] for c in con.execute("SELECT * FROM channels WHERE run_id=?", (other,))}
    titles.update({c["channel_id"]: c["title"] for c in chans})
    per = {}
    for r in g["rows"]:
        gained, k = per.get(r["channel_id"], (0, 0))
        per[r["channel_id"]] = (gained + r["gained"], k + 1)
    rows = sorted(({"title": titles.get(cid, cid), "gained": gained, "rate": gained / g["hours"], "k": k}
                   for cid, (gained, k) in per.items()), key=lambda s: -s["rate"])

    def parts(s):
        return [(f"{s['rate']:,.0f} an hour" if abs(s["rate"]) >= 10 else f"{s['rate']:.1f} an hour", INK),
                (f"across {plural(s['k'], 'upload')}", MUTED)]

    # growth() matches uploads across the two snapshots without an age limit, so older uploads count too
    first, second = (run["run_id"], other) if later else (other, run["run_id"])
    fig, ax = frame(0.98 + ROW * len(rows) + 0.72, "Views gained per hour by channel",
                    f"Between snapshots {first} and {second}, {duration(g['hours'])} apart  ·  "
                    f"{plural(len(g['rows']), 'upload')} of any age in both",
                    footer=source_line(g["prev_ts"], pair["ts"]),
                    left=OUT + (max(text_width(s["title"]) for s in rows) + GAP) / 72)
    bar_rows(ax, len(rows))
    plot_w, _ = ax_size(ax)
    vmax, vmin = max(0, max(s["rate"] for s in rows)), min(0, min(s["rate"] for s in rows))
    ticks = nice_ticks(vmax)
    xmax, xmin = max(ticks[-1], vmax), vmin
    for _ in range(12):  # extents that leave room for each end label
        u = plot_w / (xmax - xmin)
        for s in rows:
            room = (GAP + parts_width(parts(s)) + 2) / u
            if s["rate"] >= 0:
                xmax = max(xmax, s["rate"] + room)
            else:
                xmin = min(xmin, s["rate"] - room)
    for i, s in enumerate(rows):
        row_label(ax, i, [(s["title"], INK)])
        ax.barh(i, s["rate"], height=0.62, color=BAR, zorder=3)
        end_label(ax, s["rate"], i, parts(s), leftwards=s["rate"] < 0)
    ax.set_xlim(xmin, xmax)
    ax.set_xticks([t for t in MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]).tick_values(xmin, ticks[-1])
                   if xmin <= t <= ticks[-1]])
    ax.spines["bottom"].set_bounds(xmin, ticks[-1])
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:,.0f}"))
    if xmin < 0:
        ax.axvline(0, color=MUTED, linewidth=0.9, zorder=4)
    xlabel_under(ax, "Views gained per hour", xmin, ticks[-1])
    save(fig, "growth.png")
    print(f"  snapshots {g['prev_ts']} and {pair['ts']}, {g['hours']:.4f} hours apart, "
          f"{len(g['rows'])} uploads in both")
    for s in rows:
        print(f"  {s['title']}: {s['gained']:,} views gained, {s['rate']:,.1f} an hour across {s['k']} uploads")


# ---------------------------------------------------------------- main

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Build the README charts in docs/ from one snapshot.")
    ap.add_argument("--run", type=int, help="snapshot (run_id) to chart; default: the latest")
    args = ap.parse_args(argv)
    con = connect()
    if args.run is None:
        run = con.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    else:
        run = con.execute("SELECT * FROM runs WHERE run_id=?", (args.run,)).fetchone()
    if not run:
        sys.exit(f"No snapshot {args.run}. Snapshots older than {KEEP_DAYS} days are deleted at the start of each fetch."
                 if args.run is not None else "No data yet. Run: python channel_health.py fetch")
    chans, vids = load(con, run)
    summary = summarise(chans, vids)
    os.makedirs(DOCS, exist_ok=True)
    print(f"Snapshot {run['run_id']}, {when(run['ts'])}: {len(chans)} channels, {len(vids):,} uploads fetched, "
          f"{sum(s['n'] for s in summary):,} analysed, {sum(s['scored'] for s in summary):,} with a baseline "
          f"(font: {FONT})")
    print("1. format_mix.png")
    fig_format_mix(summary, vids, run)
    print("2. cadence.png")
    fig_cadence(summary, run)
    print("3. ratio_distribution.png")
    fig_ratio(vids, run)
    print("4. flags_by_channel.png")
    fig_flags(summary, run)
    print("5. expected_curve_example.png")
    fig_expected_curve(chans, vids, run)
    print("6. growth.png")
    fig_growth(con, run, chans)
    con.close()


if __name__ == "__main__":
    main()
