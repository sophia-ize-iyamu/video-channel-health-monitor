# Channel Health Monitor

A Python tool built on the YouTube Data API v3. It pulls the recent uploads and public statistics of a set of channels, stores each pull as a snapshot in SQLite, and writes a one-page HTML report that flags uploads doing far better or far worse than that channel's uploads usually do at the same age.

## Goal

A person looking after many channels can't open every channel's analytics each morning. This tool runs that check for ten Canadian channels (CFL, Toronto Raptors, TSN, CBC News, APTN News, Kurtis Conner, Alexandra Gater, Lilly Singh, The Weeknd and Tate McRae) in sports, news, creators and music, using only public data, and lists the recent uploads worth a closer look.

## Setup

1. Install Python 3.11 or later, then run `pip install -r requirements.txt`.
2. In Google Cloud, enable the YouTube Data API v3 and create an API key restricted to that API.
3. Save the key as a user environment variable named `YOUTUBE_API_KEY`. On Windows, search the Start menu for "Edit environment variables for your account". The code reads the key from the environment and never writes it to the database, log or report.
4. Run `python channel_health.py all` to pull a snapshot and build `report/index.html`. Run `python -m pytest -q tests` for the tests.

The channels are listed in `channels.json`, pinned by channel ID.

## Method

**Uploads.** For each channel, the tool reads the uploads playlist 50 items at a time, up to the 200 most recent uploads from the last 12 months, and requests their details from `videos.list` in batches of 50.

**Formats.** The Data API has no field that marks a Short, so the tool applies YouTube's rule for uploads: a vertical or square video up to 3 minutes long is a Short (60 seconds for uploads before 15 October 2024). Orientation comes from the player width and height the API returns. The API marks premieres the same way as live streams, so the two share one group. Scheduled and in-progress streams aren't scored, and an upload that hides its view count is left out rather than read as zero.

**Expected views.** For each channel and format with at least 8 uploads aged 1 to 365 days, the tool fits a line through log(views) against log(age) using the Theil-Sen method, which takes the median of all pairwise slopes. A few breakout uploads can't pull that line the way they would pull a least-squares fit. Expected views is the line's value at an upload's age.

**Flags.** Views on busy sports and news channels often come in at a quarter or four times the expected figure, so a fixed cut-off would flag a large share of uploads. An upload from the last 30 days is flagged only when its views are under half or over double the expected figure and the gap is more than 2 robust standard deviations (1.4826 times the median absolute deviation) from that channel's usual spread.

**Snapshots.** Every run is kept. From the second run on, the report shows each channel's views gained since the previous snapshot and the ten uploads that gained the most.

**Quota and errors.** The client counts quota units per call, retries server errors and rate limits with backoff, and stops cleanly if the daily quota runs out. Errors are logged without the request URL, so the key can't leak into the log.

## Results

First full snapshot, 15 October 2026 at 19:06 UTC:

- 10 channels and 1,169 uploads from the last 12 months, pulled in 64 API calls for 64 quota units, under 1% of the default daily quota of 10,000.
- Shorts were 500 of the 1,169 uploads (43%). The mix varied widely: 84% of Lilly Singh's uploads were Shorts, while Kurtis Conner posted none.
- The busiest channels reached the 200-upload cap quickly. CBC News's 200 most recent uploads covered 10 days, about 139 a week.
- 902 uploads from the last 30 days had a baseline. The tool flagged 60 above expected and 5 below.
- The largest breakout was a TSN Short of Gavin McKenna with 738,849 views at 9 days old, about 123 times what TSN's Shorts usually have at that age.
- Two of the five uploads below expected were APTN News language versions of the same program, translated into Plains Cree and Kanien'kéha. Each had under half the views expected for APTN's live streams and premieres at 15 days.

The first version looked channels up by @handle, and two lookups returned the wrong channel: @TSN returned a Ukrainian news channel titled ТСН, and @Raptors a 6-subscriber personal account. Channels are now pinned by ID, and any lookup by handle or search is checked against the expected channel name before its data is stored.

## Limitations

- The tool sees public data only: views, likes and comments. Watch time, click-through rate, traffic sources and revenue need the channel owner's sign-in through the YouTube Analytics API.
- Expected views come from uploads of different ages in a single snapshot, so a channel that is growing or shrinking quickly shifts its own baseline. Repeated snapshots give a cleaner measure of pace.
- Shorts are identified with YouTube's published length and orientation rule, not a label from YouTube. If the API returns no player size, the tool falls back to length alone and says so in the run log.
- Live streams and premieres can't be told apart through the Data API, so they're reported together.
- For channels that hit the 200-upload cap within days, the baseline reflects only those days.
- A flag marks an upload worth a look, not a problem. Low views can reflect a narrow audience, as with the language versions above.

## Files

- `channel_health.py`: API client, snapshot storage, scoring and report
- `channels.json`: the channels tracked, with pinned IDs
- `tests/test_channel_health.py`: 27 tests covering duration parsing, format rules, the trend fit, flagging, paging, quota handling and channel lookup
- `report/`: the latest report and chart
- `data/`: the SQLite snapshots (not committed)
