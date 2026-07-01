# Royal Road → EPUB

A small local web app that turns a RoyalRoad fiction into a single EPUB file for
offline reading.

## What it does

1. You paste a fiction URL (e.g. `https://www.royalroad.com/fiction/12345/your-story`).
2. It reads the table of contents, then fetches each chapter **one at a time**,
   with a polite delay between requests.
3. It assembles everything into a single `.epub` with a cover, title/author
   metadata, and a chapter-by-chapter table of contents.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open `http://localhost:5000` in your browser.

## Being a good citizen of RoyalRoad's servers

This tool is built to be slow on purpose:

- **Sequential fetching only.** Chapters are downloaded one at a time, never
  in parallel.
- **Minimum delay + jitter** between every request (configurable in the UI,
  3 seconds by default). This applies even to the very first request.
- **Automatic backoff.** If RoyalRoad responds with a 429 (rate limited) or
  503 (overloaded), the tool backs off exponentially and honors any
  `Retry-After` header the server sends, instead of retrying immediately.
- **A hard chapter cap** per job, to avoid an accidental runaway job.

You can slow it down further in the UI if you want to be extra conservative,
but you generally shouldn't need to speed it up.

## Optional login

If you provide your RoyalRoad email/password, the tool logs in and reuses that
session for all subsequent requests — this only matters if there's content on
the fiction that your *own account* already has permission to see (for
example, chapters you've unlocked early as a supporter). It does **not**
attempt to bypass paywalls, solve CAPTCHAs, or access anything your account
doesn't already have legitimate access to. If RoyalRoad presents a CAPTCHA or
other challenge, login will simply fail and the app will tell you.

Credentials are only held in memory for the life of that one conversion job.
They are never written to disk, logged, or sent anywhere other than
royalroad.com's own login endpoint.

## Please use this responsibly

- Only convert fictions you have the right to read, for your own personal
  offline reading.
- Don't redistribute the generated EPUB.
- Respect RoyalRoad's Terms of Service. If in doubt, ask the author, or just
  read on the site.

## If RoyalRoad changes their page layout

The scraper (`rr_client.py`) first looks for an embedded JSON chapter list
in the fiction page's `<script>` tags, and falls back to scraping the visible
chapter table/links if that isn't found. If RoyalRoad ships a layout change
that breaks both paths, you'll see a clear error message rather than garbage
output — the CSS selectors to update are collected near the top of
`rr_client.py` (`_extract_chapters_json`, `_extract_chapters_table`, and the
`.chapter-content` selector in `get_chapter`).

## Files

- `app.py` — Flask app, background job orchestration, API endpoints
- `rr_client.py` — login + scraping logic
- `rate_limiter.py` — the pacing/backoff logic described above
- `epub_builder.py` — assembles the final `.epub` with `ebooklib`
- `templates/index.html` — the UI
