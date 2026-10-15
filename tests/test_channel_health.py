import math
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import channel_health as ch

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("iso,seconds", [
    ("PT45S", 45), ("PT3M", 180), ("PT1H2M3S", 3723), ("P1DT1H", 90000), ("PT0S", 0), ("", 0), (None, 0), ("bad", 0),
])
def test_parse_duration(iso, seconds):
    assert ch.parse_duration(iso) == seconds


def _row(dur=100, w=563, h=1000, live=0, bc="none", published="2026-10-01T00:00:00Z"):
    return dict(duration_s=dur, width=w, height=h, live_details=live, broadcast=bc, published=published)


def test_classify_shorts_rule():
    assert ch.classify(_row(dur=178)) == "Shorts"                       # vertical, 3 minutes or less
    assert ch.classify(_row(dur=60, w=1000, h=1000)) == "Shorts"        # square counts
    assert ch.classify(_row(dur=178, w=1000, h=563)) == "Videos"        # horizontal music video
    assert ch.classify(_row(dur=181)) == "Videos"                       # over 3 minutes
    assert ch.classify(_row(dur=120, published="2024-06-01T00:00:00Z")) == "Videos"   # 60 s limit back then
    assert ch.classify(_row(dur=45, published="2024-06-01T00:00:00Z")) == "Shorts"
    assert ch.classify(_row(dur=100, w=None, h=None)) == "Shorts"      # no player size: length alone
    assert ch.classify(_row(dur=0)) == "Videos"


def test_classify_live_and_scheduled():
    assert ch.classify(_row(dur=246, w=1000, h=563, live=1)) == "Live and premieres"
    assert ch.classify(_row(live=1, bc="upcoming")) == ch.NOT_SCORED
    assert ch.classify(_row(live=1, bc="live")) == ch.NOT_SCORED


def test_parse_video_keeps_missing_counts_as_none():
    v = {"id": "x", "snippet": {"title": "t", "publishedAt": "2026-10-01T00:00:00Z", "liveBroadcastContent": "none"},
         "contentDetails": {"duration": "PT2M"}, "statistics": {"likeCount": "5"},
         "player": {"embedWidth": "563", "embedHeight": "1000"}}
    r = ch.parse_video(v)
    assert r["views"] is None and r["comments"] is None and r["likes"] == 5
    assert (r["width"], r["height"], r["duration_s"], r["live_details"]) == (563, 1000, 120, 0)


def test_parse_video_upcoming_has_no_duration():
    v = {"id": "y", "snippet": {"title": "t", "publishedAt": "2026-10-20T00:00:00Z", "liveBroadcastContent": "upcoming"},
         "contentDetails": {}, "statistics": {}, "liveStreamingDetails": {"scheduledStartTime": "2026-10-20T00:00:00Z"}}
    r = ch.parse_video(v)
    assert r["duration_s"] == 0 and r["broadcast"] == "upcoming" and ch.classify(r) == ch.NOT_SCORED


@pytest.mark.parametrize("expected,title,ok", [
    ("CBC News", "CBC News", True), ("TSN", "TSN", True), ("Montreal", "Montréal Alouettes", True),
    ("TSN", "ТСН", False),       # the Ukrainian channel a handle lookup returned
    ("Toronto Raptors", "raptors", False),      # the 6-subscriber channel a handle lookup returned
])
def test_name_matches(expected, title, ok):
    assert ch.name_matches(expected, title) is ok


def test_theil_sen_recovers_line_and_ignores_outliers():
    xs = [float(i) for i in range(10)]
    ys = [2 + 0.5 * x for x in xs]
    a, b = ch.theil_sen(xs, ys)
    assert a == pytest.approx(2) and b == pytest.approx(0.5)
    ys[3] += 50   # one breakout
    ys[7] -= 40   # one flop
    a, b = ch.theil_sen(xs, ys)
    assert a == pytest.approx(2) and b == pytest.approx(0.5)
    assert ch.theil_sen([1.0, 1.0], [2.0, 3.0]) is None


def _video(vid, age_days, views, fmt="Videos", channel="A"):
    pub = (NOW - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")
    return dict(video_id=vid, channel_id=channel, fmt=fmt, views=views, published=pub)


def _on_curve(age):  # views grow with the square root of age
    return round(math.expm1(math.log(1000) + 0.5 * math.log(age)))


def test_score_flags_recent_uploads_against_age_adjusted_baseline():
    ages = [2, 4, 6, 9, 12, 20, 40, 80, 150, 300]
    rows = [_video(f"v{a}", a, _on_curve(a)) for a in ages]
    rows.append(_video("flop", 5, round(_on_curve(5) * 0.3)))
    rows.append(_video("hit", 10, round(_on_curve(10) * 3)))
    rows.append(_video("old_flop", 100, round(_on_curve(100) * 0.3)))   # outside the 30-day flag window
    rows.append(_video("too_new", 0.5, 10))
    rows.append(_video("hidden", 5, None))
    out = {r["video_id"]: r for r in ch.score_videos(rows, NOW)}
    assert out["flop"]["flag"] == "below expected" and out["flop"]["ratio"] == pytest.approx(0.3, rel=0.02)
    assert out["hit"]["flag"] == "above expected" and out["hit"]["ratio"] == pytest.approx(3, rel=0.02)
    assert out["old_flop"]["flag"] == "" and out["old_flop"]["ratio"] < ch.LOW
    assert out["v9"]["flag"] == "" and out["v9"]["ratio"] == pytest.approx(1, rel=0.01)
    assert out["too_new"]["expected"] is None and out["too_new"]["flag"] == ""
    assert out["hidden"]["expected"] is None and out["hidden"]["flag"] == ""


def test_score_needs_enough_uploads_and_separates_formats():
    ages = [2, 4, 6, 9, 12, 20, 40]                       # 7 videos: one short of a baseline
    rows = [_video(f"v{a}", a, _on_curve(a)) for a in ages]
    rows += [_video(f"s{a}", a, 100 * _on_curve(a), fmt="Shorts") for a in ages + [60]]
    out = ch.score_videos(rows, NOW)
    assert all(r["expected"] is None for r in out if r["fmt"] == "Videos")
    assert all(r["flag"] == "" for r in out)              # Shorts at 100x aren't judged against videos
    assert all(r["ratio"] == pytest.approx(1, rel=0.01) for r in out if r["fmt"] == "Shorts")


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.params = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.params.append(dict(params))
        return self.responses.pop(0)


def _err(reason):
    return {"error": {"errors": [{"reason": reason}]}}


def test_client_counts_quota_and_retries(monkeypatch):
    monkeypatch.setattr(ch.time, "sleep", lambda s: None)
    c = ch.Client("k", FakeSession([FakeResp(500, {}), FakeResp(200, {"items": []})]))
    assert c.get("videos", id="x") == {"items": []}
    assert c.calls == 2 and c.units == 2
    c2 = ch.Client("k", FakeSession([FakeResp(200, {"items": []})]))
    c2.get("search", q="x")
    assert c2.units == 100


def test_client_stops_on_quota():
    c = ch.Client("k", FakeSession([FakeResp(403, _err("quotaExceeded"))]))
    with pytest.raises(ch.QuotaExceeded):
        c.get("videos", id="x")


def test_client_log_never_contains_key():
    c = ch.Client("SECRET123", FakeSession([FakeResp(400, _err("badRequest"))]))
    assert c.get("videos", id="x") is None
    assert "badRequest" in c.errors[0] and "SECRET123" not in " ".join(c.errors)


def _page(n, start, published, token):
    items = [{"contentDetails": {"videoId": f"v{start + i}", "videoPublishedAt": published}} for i in range(n)]
    return FakeResp(200, {"items": items, **({"nextPageToken": token} if token else {})})


def test_upload_paging_stops_at_cap():
    pages = [_page(50, i * 50, "2026-10-01T00:00:00Z", f"t{i}") for i in range(5)]
    s = FakeSession(pages)
    ids = ch.recent_upload_ids(ch.Client("k", s), "UU1", NOW)
    assert len(ids) == ch.MAX_VIDEOS and len(s.params) == 4 and s.params[1]["pageToken"] == "t0"


def test_upload_paging_stops_past_history_window():
    s = FakeSession([_page(50, 0, "2026-09-01T00:00:00Z", "t0"), _page(50, 50, "2025-01-01T00:00:00Z", "t1"),
                     _page(50, 100, "2024-01-01T00:00:00Z", None)])
    ids = ch.recent_upload_ids(ch.Client("k", s), "UU1", NOW)
    assert len(ids) == 100 and len(s.params) == 2


def test_resolve_prefers_pinned_id():
    s = FakeSession([FakeResp(200, {"items": [{"id": "UC1", "snippet": {"title": "CFL"}}]})])
    found, how = ch.resolve_channel(ch.Client("k", s), {"name": "CFL", "id": "UC1", "handle": "@CFL"})
    assert how == "id" and found["id"] == "UC1" and "id" in s.params[0] and "forHandle" not in s.params[0]


def test_noisy_channel_needs_an_unusual_gap_not_just_double():
    ages = [2, 3, 4, 5, 6, 8, 10, 12, 15, 20]
    mult = [1.3, 0.25, 2.5, 0.8, 1, 4, 0.6, 1, 0.4, 1.7]   # views normally swing 4x either way
    rows = [_video(f"n{a}", a, round(_on_curve(a) * m)) for a, m in zip(ages, mult)]
    rows += [_video("mild", 7, round(_on_curve(7) * 2.5)), _video("huge", 9, round(_on_curve(9) * 20)),
             _video("deep", 11, round(_on_curve(11) / 20))]
    out = {r["video_id"]: r for r in ch.score_videos(rows, NOW)}
    assert out["mild"]["ratio"] > ch.HIGH and out["mild"]["flag"] == ""     # double, but normal for this channel
    assert out["huge"]["flag"] == "above expected"
    assert out["deep"]["flag"] == "below expected"
