"""
Builds an EPUB from scraped fiction metadata + chapter HTML using ebooklib.
"""

import uuid

from ebooklib import epub


def build_epub(fiction, chapters, output_path, cover_bytes=None):
    """
    fiction: {"title", "author", "description_html"}
    chapters: [{"title": str, "content_html": str}, ...]
    """
    book = epub.EpubBook()

    book.set_identifier(str(uuid.uuid4()))
    book.set_title(fiction["title"])
    book.set_language("en")
    book.add_author(fiction.get("author", "Unknown"))

    if fiction.get("description_html"):
        book.add_metadata("DC", "description", fiction["description_html"])

    if cover_bytes:
        book.set_cover("cover.jpg", cover_bytes)

    epub_chapters = []
    for idx, chap in enumerate(chapters, start=1):
        file_name = f"chap_{idx:04d}.xhtml"
        c = epub.EpubHtml(title=chap["title"], file_name=file_name, lang="en")
        c.content = (
            f"<h1>{_escape(chap['title'])}</h1>\n{chap['content_html']}"
        )
        book.add_item(c)
        epub_chapters.append(c)

    book.toc = tuple(epub_chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    style = """
    body { font-family: Georgia, serif; line-height: 1.5; margin: 1em; }
    h1 { font-size: 1.4em; margin-bottom: 1em; }
    p { margin: 0 0 1em 0; }
    """
    nav_css = epub.EpubItem(
        uid="style_nav",
        file_name="style/nav.css",
        media_type="text/css",
        content=style,
    )
    book.add_item(nav_css)
    for c in epub_chapters:
        c.add_item(nav_css)

    book.spine = ["nav"] + epub_chapters

    epub.write_epub(output_path, book)
    return output_path


def _escape(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
