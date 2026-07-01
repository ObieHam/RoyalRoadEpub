"""
RoyalRoad client: optional login + scraping of the fiction table-of-contents
page and individual chapter pages.

Notes on the "new layout":
- The fiction overview page (https://www.royalroad.com/fiction/<id>/<slug>)
  embeds the full chapter list as a JSON blob inside a <script> tag
  (a `window.chapters = [...]` style assignment). We prefer parsing that
  JSON because it's far more stable than scraping the rendered HTML table,
  which is mostly there for visual presentation and can shift between
  layout revisions.
- If that embedded JSON isn't found (layout changed again, or a given page
  doesn't include it), we fall back to scraping the visible chapter table.
- Chapter reading pages render the prose inside a container whose class
  includes "chapter-content" or "chapter-inner". RoyalRoad is known to
  inject a small number of hidden/invisible paragraphs into chapter
  content as a copy-trap for scrapers who don't render CSS. We strip any
  element with inline `display:none`, `visibility:hidden`, or class names
  that clearly mark them as such, so those decoy paragraphs never end up
  in the generated EPUB.
"""

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

BASE_URL = "https://www.royalroad.com"

DEFAULT_HEADERS = {
    # A plain, honest identifier + a normal browser Accept header.
    # We are not trying to impersonate a specific browser fingerprint;
    # we just want normal HTML back.
    "User-Agent": (
        "Mozilla/5.0 (compatible; RoyalRoad-EPUB-Tool/1.0; "
        "personal offline-reading tool)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class RoyalRoadError(Exception):
    pass


class LoginError(RoyalRoadError):
    pass


class RoyalRoadClient:
    def __init__(self, session, rate_limiter, log=None):
        self.session = session
        self.session.headers.update(DEFAULT_HEADERS)
        self.limiter = rate_limiter
        self.log = log or (lambda msg: None)

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #

    def login(self, email, password):
        """
        Log in with the user's own RoyalRoad account so the session can
        access anything that account already has permission to see
        (e.g. early-access chapters the user has unlocked as a patron).

        This does not attempt to defeat CAPTCHAs or any other anti-abuse
        challenge -- if RoyalRoad presents one, this will simply fail and
        report that login didn't succeed.
        """
        login_url = f"{BASE_URL}/account/login"

        resp = self.limiter.request(
            lambda: self.session.get(login_url, timeout=20),
            description="GET login page",
        )
        if resp.status_code != 200:
            raise LoginError(f"Could not load login page (HTTP {resp.status_code})")

        soup = BeautifulSoup(resp.text, "html.parser")
        form = soup.find("form")
        if form is None:
            raise LoginError("Could not find a login form on the login page")

        payload = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            input_type = (inp.get("type") or "text").lower()
            if input_type == "checkbox":
                if inp.get("checked") is not None:
                    payload[name] = inp.get("value", "on")
                continue
            payload[name] = inp.get("value", "")

        email_field = None
        password_field = None
        for inp in form.find_all("input"):
            name = inp.get("name") or ""
            input_type = (inp.get("type") or "").lower()
            if input_type == "password":
                password_field = name
            elif input_type == "email" or re.search(r"email", name, re.I):
                email_field = name

        if not email_field or not password_field:
            raise LoginError(
                "Could not identify the email/password fields on the login form "
                "(RoyalRoad may have changed the login page layout)"
            )

        payload[email_field] = email
        payload[password_field] = password

        action = form.get("action") or login_url
        post_url = urljoin(login_url, action)

        post_resp = self.limiter.request(
            lambda: self.session.post(
                post_url, data=payload, timeout=20, allow_redirects=True
            ),
            description="POST login form",
        )

        # A successful login normally redirects away from /account/login.
        if "/account/login" in post_resp.url:
            raise LoginError(
                "Login did not succeed -- check the email/password, and note "
                "this tool cannot solve CAPTCHAs if one was presented."
            )

        self.log("Logged in successfully.")
        return True

    # ------------------------------------------------------------------ #
    # Fiction table of contents
    # ------------------------------------------------------------------ #

    def get_fiction(self, fiction_url):
        """
        Returns:
            {
                "title": str,
                "author": str,
                "description_html": str,
                "cover_url": str | None,
                "chapters": [{"title": str, "url": str}, ...],
            }
        """
        resp = self.limiter.request(
            lambda: self.session.get(fiction_url, timeout=20),
            description=f"GET fiction page {fiction_url}",
        )
        if resp.status_code != 200:
            raise RoyalRoadError(
                f"Could not load fiction page (HTTP {resp.status_code})"
            )

        soup = BeautifulSoup(resp.text, "html.parser")

        title = self._first_text(soup, ["h1.font-white", "h1"]) or "Untitled"
        author = self._extract_author(soup)
        description_html = self._extract_description(soup)
        cover_url = self._extract_cover(soup, fiction_url)
        chapters = self._extract_chapters_json(resp.text, fiction_url)

        if not chapters:
            chapters = self._extract_chapters_table(soup, fiction_url)

        if not chapters:
            raise RoyalRoadError(
                "Could not find any chapters on this page -- RoyalRoad may "
                "have changed its page layout again."
            )

        return {
            "title": title.strip(),
            "author": author.strip() if author else "Unknown",
            "description_html": description_html,
            "cover_url": cover_url,
            "chapters": chapters,
        }

    def _first_text(self, soup, selectors):
        for sel in selectors:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        return None

    def _extract_author(self, soup):
        el = soup.select_one('a[href*="/profile/"]')
        if el:
            return el.get_text(strip=True)
        meta = soup.find("meta", attrs={"name": "twitter:creator"})
        if meta and meta.get("content"):
            return meta["content"]
        return "Unknown"

    def _extract_description(self, soup):
        el = soup.select_one(".description") or soup.select_one(
            '[property="description"]'
        )
        if el:
            return str(el)
        return ""

    def _extract_cover(self, soup, fiction_url):
        meta = soup.find("meta", attrs={"property": "og:image"})
        if meta and meta.get("content"):
            return meta["content"]
        img = soup.select_one(".fic-header img") or soup.select_one("img.thumbnail")
        if img and img.get("src"):
            return urljoin(fiction_url, img["src"])
        return None

    def _extract_chapters_json(self, html_text, fiction_url):
        """Look for an embedded `window.chapters = [...]` (or similar)
        JSON assignment and parse chapter url/title pairs out of it."""
        chapters = []
        patterns = [
            r"window\.chapters\s*=\s*(\[.*?\])\s*;",
            r"var\s+chapters\s*=\s*(\[.*?\])\s*;",
        ]
        for pattern in patterns:
            match = re.search(pattern, html_text, re.S)
            if not match:
                continue
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            for item in data:
                url = item.get("url") or item.get("chapterUrl") or item.get("link")
                title = item.get("title") or item.get("chapterTitle") or item.get("name")
                if not url:
                    continue
                chapters.append(
                    {"title": (title or "Untitled Chapter").strip(), "url": urljoin(fiction_url, url)}
                )
            if chapters:
                return chapters
        return chapters

    def _extract_chapters_table(self, soup, fiction_url):
        """Fallback: scrape the visible chapter table/list."""
        chapters = []
        table = soup.find(id="chapters") or soup.select_one("table.table")
        rows = []
        if table:
            rows = table.select("tr[data-url], tr")
        if not rows:
            # Broadest fallback: any link that looks like a chapter link.
            for a in soup.select('a[href*="/chapter/"]'):
                href = a.get("href")
                if not href:
                    continue
                text = a.get_text(strip=True)
                if text:
                    chapters.append({"title": text, "url": urljoin(fiction_url, href)})
            # de-duplicate, preserve order
            seen = set()
            deduped = []
            for c in chapters:
                if c["url"] not in seen:
                    seen.add(c["url"])
                    deduped.append(c)
            return deduped

        for row in rows:
            url = row.get("data-url")
            link = row.select_one('a[href*="/chapter/"]')
            if not url and link:
                url = link.get("href")
            if not url:
                continue
            title = (link.get_text(strip=True) if link else row.get_text(strip=True))
            if title:
                chapters.append({"title": title, "url": urljoin(fiction_url, url)})
        return chapters

    # ------------------------------------------------------------------ #
    # Individual chapters
    # ------------------------------------------------------------------ #

    def get_chapter(self, chapter_url):
        """Returns {"title": str, "content_html": str}."""
        resp = self.limiter.request(
            lambda: self.session.get(chapter_url, timeout=20),
            description=f"GET chapter {chapter_url}",
        )
        if resp.status_code != 200:
            raise RoyalRoadError(
                f"Could not load chapter (HTTP {resp.status_code}): {chapter_url}"
            )

        soup = BeautifulSoup(resp.text, "html.parser")

        title = self._first_text(soup, ["h1"]) or "Chapter"

        content_el = (
            soup.select_one(".chapter-content")
            or soup.select_one(".chapter-inner .chapter-content")
            or soup.select_one('[property="chapterContent"]')
        )
        if content_el is None:
            raise RoyalRoadError(
                f"Could not find chapter content container for {chapter_url} "
                "-- layout may have changed."
            )

        self._strip_decoys(content_el)

        return {"title": title.strip(), "content_html": str(content_el)}

    def _strip_decoys(self, content_el):
        """Remove hidden/zero-size elements RoyalRoad sometimes injects as
        a scraper trap, so they never end up in the EPUB."""
        for el in content_el.find_all(True):
            style = (el.get("style") or "").lower().replace(" ", "")
            if "display:none" in style or "visibility:hidden" in style or "opacity:0" in style:
                el.decompose()
                continue
            classes = " ".join(el.get("class") or [])
            if re.search(r"\b(hidden|d-none|sr-only)\b", classes, re.I):
                el.decompose()
