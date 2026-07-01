"""
RoyalRoad -> EPUB web app.

Flow:
  1. POST /api/preview   -- loads the fiction page (+ logs in first, if
     credentials were given), returns title/author/cover/description and
     the FULL chapter list (titles only) so the UI can show a total count
     and let the user pick a chapter range. The logged-in session and
     scraped data are cached server-side under a preview_id.
  2. POST /api/convert   -- takes a preview_id + a chapter range + a few
     toggles (include cover, include description as first chapter,
     disable inter-chapter delay), starts a background job that reuses
     the cached session, and returns a job_id.
  3. GET  /api/status/<job_id>   -- poll progress.
  4. GET  /api/download/<job_id> -- fetch the finished .epub.

Safety/etiquette measures baked in:
  - Sequential fetching only (no concurrent hammering) via RateLimiter
  - Minimum delay + jitter between every request by default; the delay
    can be turned off by the user, but the backoff/retry safety net
    (honoring 429/503 + Retry-After) is never disabled -- that part
    protects RoyalRoad's servers regardless of user preference.
  - A hard cap on chapters per job to prevent runaway/accidental abuse
  - Credentials are only held in memory for the duration of the preview
    session and are never written to disk or logged
  - Preview sessions (and their cached data / HTTP sessions) expire and
    are cleaned up automatically after a couple of hours
"""

import os
import re
import threading
import time
import uuid
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request, send_file, abort

from rate_limiter import RateLimiter
from rr_client import RoyalRoadClient, RoyalRoadError, LoginError
from epub_builder import build_epub

app = Flask(__name__)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "generated")
os.makedirs(OUTPUT_DIR, exist_ok=True)

MAX_CHAPTERS_PER_JOB = 3000  # hard safety cap
PREVIEW_TTL_SECONDS = 2 * 60 * 60  # 2 hours
JOB_TTL_SECONDS = 2 * 60 * 60

JOBS = {}
JOBS_LOCK = threading.Lock()

PREVIEWS = {}
PREVIEWS_LOCK = threading.Lock()


# ---------------------------------------------------------------------- #
# Small helpers
# ---------------------------------------------------------------------- #

def _set_job(job_id, **updates):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(updates)


def _get_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def _cleanup_expired(now=None):
    now = now or time.time()
    with PREVIEWS_LOCK:
        stale = [
            pid for pid, p in PREVIEWS.items()
            if now - p["created_at"] > PREVIEW_TTL_SECONDS
        ]
        for pid in stale:
            try:
                PREVIEWS[pid]["session"].close()
            except Exception:
                pass
            del PREVIEWS[pid]
    with JOBS_LOCK:
        stale_jobs = [
            jid for jid, j in JOBS.items()
            if now - j.get("created_at", now) > JOB_TTL_SECONDS
        ]
        for jid in stale_jobs:
            file_path = JOBS[jid].get("file_path")
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            del JOBS[jid]


def _validate_fiction_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.netloc.lower()
    if host not in ("royalroad.com", "www.royalroad.com"):
        return False
    if "/fiction/" not in parsed.path:
        return False
    return True


# ---------------------------------------------------------------------- #
# Preview: load fiction metadata + chapter list, cache the session
# ---------------------------------------------------------------------- #

@app.route("/api/preview", methods=["POST"])
def preview():
    _cleanup_expired()

    data = request.get_json(silent=True) or {}
    fiction_url = (data.get("url") or "").strip()
    email = (data.get("email") or "").strip() or None
    password = data.get("password") or None

    if not fiction_url or not _validate_fiction_url(fiction_url):
        return jsonify({"error": "Please provide a valid royalroad.com fiction URL."}), 400

    session = requests.Session()
    # A modest, fixed pace for the (one or two) preview requests. The
    # user's chosen delay only applies to the bulk chapter-download job.
    limiter = RateLimiter(min_delay=2.0, jitter=1.0)
    client = RoyalRoadClient(session, limiter, log=lambda msg: None)

    if email and password:
        try:
            client.login(email, password)
        except LoginError as exc:
            session.close()
            return jsonify({"error": f"Login failed: {exc}"}), 400

    try:
        fiction = client.get_fiction(fiction_url)
    except RoyalRoadError as exc:
        session.close()
        return jsonify({"error": str(exc)}), 400

    preview_id = uuid.uuid4().hex
    with PREVIEWS_LOCK:
        PREVIEWS[preview_id] = {
            "session": session,
            "fiction_url": fiction_url,
            "fiction": fiction,
            "logged_in": bool(email and password),
            "created_at": time.time(),
        }

    total = len(fiction["chapters"])
    return jsonify({
        "preview_id": preview_id,
        "title": fiction["title"],
        "author": fiction["author"],
        "cover_url": fiction.get("cover_url"),
        "has_description": bool(fiction.get("description_html")),
        "total_chapters": total,
        "chapter_titles": [c["title"] for c in fiction["chapters"]],
        "logged_in": bool(email and password),
    })


# ---------------------------------------------------------------------- #
# Convert: download the chosen chapter range and build the EPUB
# ---------------------------------------------------------------------- #

def _run_job(job_id, preview_id, start_idx, end_idx, disable_delay, min_delay,
             include_cover, include_description):
    def log(msg):
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["log"].append(msg)
                JOBS[job_id]["log"] = JOBS[job_id]["log"][-200:]

    try:
        with PREVIEWS_LOCK:
            preview_data = PREVIEWS.get(preview_id)

        if not preview_data:
            _set_job(job_id, status="error",
                     message="This preview has expired -- please load the fiction again.")
            return

        session = preview_data["session"]
        fiction = preview_data["fiction"]
        all_chapters = fiction["chapters"]

        chapters_meta = all_chapters[start_idx - 1:end_idx]
        chapters_meta = chapters_meta[:MAX_CHAPTERS_PER_JOB]
        total = len(chapters_meta)

        effective_delay = 0.0 if disable_delay else max(0.0, min_delay)
        limiter = RateLimiter(
            min_delay=effective_delay,
            jitter=(effective_delay * 0.6) if effective_delay else 0.0,
        )
        client = RoyalRoadClient(session, limiter, log=log)

        _set_job(
            job_id,
            status="running",
            title=fiction["title"],
            author=fiction["author"],
            total=total,
            progress=0,
        )
        if disable_delay:
            log("Delay between chapters is disabled -- fetching as fast as the server allows.")
        log(f"Downloading chapters {start_idx}-{end_idx} of '{fiction['title']}' ({total} total).")

        cover_bytes = None
        if include_cover and fiction.get("cover_url"):
            try:
                resp = limiter.request(
                    lambda: session.get(fiction["cover_url"], timeout=20),
                    description="GET cover image",
                )
                if resp.status_code == 200:
                    cover_bytes = resp.content
                    log("Downloaded cover image.")
            except Exception as exc:
                log(f"Could not fetch cover image, continuing without it ({exc}).")

        chapters = []
        if include_description and fiction.get("description_html"):
            chapters.append({"title": "Synopsis", "content_html": fiction["description_html"]})
            log("Added novel description as an introductory chapter.")

        for i, meta in enumerate(chapters_meta, start=1):
            job_now = _get_job(job_id)
            if job_now and job_now.get("cancel"):
                _set_job(job_id, status="cancelled", message="Cancelled by user.")
                log("Job cancelled.")
                return

            log(f"Fetching chapter {i}/{total}: {meta['title']}")
            try:
                chap = client.get_chapter(meta["url"])
            except RoyalRoadError as exc:
                log(f"Skipping chapter '{meta['title']}' ({exc})")
                continue

            chapters.append(chap)
            _set_job(job_id, progress=i)

        if not chapters or (len(chapters) == 1 and chapters[0]["title"] == "Synopsis"):
            _set_job(job_id, status="error", message="No chapters could be downloaded.")
            return

        log("Building EPUB...")
        safe_name = re.sub(r"[^\w\- ]", "", fiction["title"]).strip() or "book"
        file_name = f"{safe_name}-{job_id[:8]}.epub"
        output_path = os.path.join(OUTPUT_DIR, file_name)

        build_epub(fiction, chapters, output_path, cover_bytes=cover_bytes)

        log("Done.")
        _set_job(
            job_id,
            status="done",
            file_path=output_path,
            file_name=f"{safe_name}.epub",
            progress=total,
        )

    except RoyalRoadError as exc:
        _set_job(job_id, status="error", message=str(exc))
    except Exception as exc:  # last-resort safety net so the UI doesn't hang
        _set_job(job_id, status="error", message=f"Unexpected error: {exc}")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/convert", methods=["POST"])
def convert():
    _cleanup_expired()

    data = request.get_json(silent=True) or {}
    preview_id = (data.get("preview_id") or "").strip()

    with PREVIEWS_LOCK:
        preview_data = PREVIEWS.get(preview_id)

    if not preview_data:
        return jsonify({"error": "This preview has expired -- please load the fiction again."}), 400

    total_chapters = len(preview_data["fiction"]["chapters"])

    try:
        start_idx = int(data.get("start", 1))
        end_idx = int(data.get("end", total_chapters))
    except (TypeError, ValueError):
        return jsonify({"error": "Chapter range must be numbers."}), 400

    if start_idx < 1 or end_idx > total_chapters or start_idx > end_idx:
        return jsonify({
            "error": f"Chapter range must be between 1 and {total_chapters}, with start <= end."
        }), 400

    if (end_idx - start_idx + 1) > MAX_CHAPTERS_PER_JOB:
        return jsonify({
            "error": f"That range covers more than the {MAX_CHAPTERS_PER_JOB}-chapter safety cap per job."
        }), 400

    disable_delay = bool(data.get("disable_delay"))
    try:
        min_delay = max(0.0, float(data.get("min_delay", 3.0)))
    except (TypeError, ValueError):
        min_delay = 3.0

    include_cover = data.get("include_cover", True)
    include_description = data.get("include_description", True)

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "starting",
            "progress": 0,
            "total": 0,
            "log": [],
            "cancel": False,
            "created_at": time.time(),
        }

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, preview_id, start_idx, end_idx, disable_delay, min_delay,
              include_cover, include_description),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job id."}), 404

    safe = {
        "status": job.get("status"),
        "progress": job.get("progress", 0),
        "total": job.get("total", 0),
        "title": job.get("title"),
        "author": job.get("author"),
        "message": job.get("message"),
        "log": job.get("log", [])[-20:],
        "download_ready": job.get("status") == "done",
    }
    return jsonify(safe)


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job id."}), 404
    _set_job(job_id, cancel=True)
    return jsonify({"ok": True})


@app.route("/api/download/<job_id>")
def download(job_id):
    job = _get_job(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    return send_file(
        job["file_path"],
        as_attachment=True,
        download_name=job.get("file_name", "book.epub"),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
