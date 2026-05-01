#!/usr/bin/env python3
"""
Mirror Claude blog posts as Markdown files.

The Claude blog is a Webflow site, so this script fetches the full HTML with a
browser-like User-Agent, discovers article URLs from the sitemap, and extracts
the article body from the rendered Webflow rich-text block.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://claude.com"
BLOG_INDEX_URL = f"{BASE_URL}/blog"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
MANIFEST_FILE = "blog_manifest.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 Claude-Blog-Mirror/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0
RATE_LIMIT_SECONDS = 0.5

MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlogPost:
    url: str
    slug: str
    title: str
    description: str
    image: str
    date_published_raw: str
    date_modified_raw: str
    date_published: datetime
    body_markdown: str


class BlogBodyMarkdownParser(HTMLParser):
    """Extract and convert the Claude blog rich-text body to Markdown."""

    TARGET_CLASS = "u-rich-text-blog"
    SKIP_TAGS = {"script", "style", "svg", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.capturing = False
        self.div_depth = 0
        self.skip_depth = 0
        self.parts: List[str] = []
        self.link_stack: List[str] = []
        self.list_stack: List[Dict[str, int | str]] = []
        self.in_pre = False
        self.in_inline_code = False
        self.heading_level: Optional[int] = None
        self.found_body = False

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = self._attrs_to_dict(attrs)

        if not self.capturing:
            if tag == "div" and self.TARGET_CLASS in attrs_dict.get("class", "").split():
                self.capturing = True
                self.found_body = True
                self.div_depth = 1
            return

        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return

        if tag == "div":
            self.div_depth += 1
            return

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading_level = int(tag[1])
            self._ensure_newlines(2)
            self._write("#" * self.heading_level + " ")
        elif tag == "p":
            self._ensure_newlines(2)
        elif tag == "br":
            self._write("\n")
        elif tag in {"strong", "b"}:
            self._write("**")
        elif tag in {"em", "i"}:
            self._write("*")
        elif tag == "a":
            href = attrs_dict.get("href", "")
            self.link_stack.append(href)
            self._write("[")
        elif tag == "img":
            src = attrs_dict.get("src", "")
            alt = attrs_dict.get("alt", "")
            if src:
                self._write(f"![{self._escape_brackets(alt)}]({src})")
        elif tag in {"ul", "ol"}:
            self._ensure_newlines(1)
            self.list_stack.append({"type": tag, "counter": 1})
        elif tag == "li":
            self._ensure_newlines(1)
            indent = "  " * max(0, len(self.list_stack) - 1)
            marker = "- "
            if self.list_stack and self.list_stack[-1]["type"] == "ol":
                counter = int(self.list_stack[-1]["counter"])
                marker = f"{counter}. "
                self.list_stack[-1]["counter"] = counter + 1
            self._write(indent + marker)
        elif tag == "blockquote":
            self._ensure_newlines(2)
            self._write("> ")
        elif tag == "pre":
            self._ensure_newlines(2)
            self.in_pre = True
            self._write("```\n")
        elif tag == "code":
            if not self.in_pre:
                self.in_inline_code = True
                self._write("`")

    def handle_endtag(self, tag: str) -> None:
        if not self.capturing:
            return

        if self.skip_depth:
            if tag in self.SKIP_TAGS:
                self.skip_depth -= 1
            return

        if tag == "div":
            if self.div_depth == 1:
                self.capturing = False
                self.div_depth = 0
            else:
                self.div_depth -= 1
            return

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading_level = None
            self._ensure_newlines(2)
        elif tag == "p":
            self._ensure_newlines(2)
        elif tag in {"strong", "b"}:
            self._write("**")
        elif tag in {"em", "i"}:
            self._write("*")
        elif tag == "a":
            href = self.link_stack.pop() if self.link_stack else ""
            if href:
                self._write(f"]({href})")
            else:
                self._write("]")
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self._ensure_newlines(2)
        elif tag == "li":
            self._ensure_newlines(1)
        elif tag == "blockquote":
            self._ensure_newlines(2)
        elif tag == "pre":
            if not self._endswith("\n"):
                self._write("\n")
            self._write("```\n")
            self.in_pre = False
            self._ensure_newlines(2)
        elif tag == "code":
            if self.in_inline_code:
                self._write("`")
                self.in_inline_code = False

    def handle_data(self, data: str) -> None:
        if not self.capturing or self.skip_depth:
            return

        text = html.unescape(data).replace("\xa0", " ").replace("\u200d", "")
        if self.in_pre:
            self._write(text)
            return

        text = re.sub(r"\s+", " ", text)
        if not text:
            return

        if data[:1].isspace() and self.parts and not self._endswith((" ", "\n")):
            text = " " + text.lstrip()
        elif self._at_line_start():
            text = text.lstrip()

        self._write(text)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    def get_markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"

    @staticmethod
    def _attrs_to_dict(attrs: Sequence[Tuple[str, Optional[str]]]) -> Dict[str, str]:
        return {key: value or "" for key, value in attrs}

    @staticmethod
    def _escape_brackets(value: str) -> str:
        return value.replace("[", "\\[").replace("]", "\\]")

    def _write(self, value: str) -> None:
        self.parts.append(value)

    def _endswith(self, suffix: str | Tuple[str, ...]) -> bool:
        return "".join(self.parts).endswith(suffix)

    def _at_line_start(self) -> bool:
        text = "".join(self.parts)
        return not text or text.endswith("\n")

    def _ensure_newlines(self, count: int) -> None:
        text = "".join(self.parts).rstrip(" \t")
        self.parts = [text]
        current = len(text) - len(text.rstrip("\n"))
        if current < count:
            self.parts.append("\n" * (count - current))


def fetch_text(url: str) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            request = Request(url, headers=HEADERS)
            with urlopen(request, timeout=30) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, "replace")
        except HTTPError as exc:
            if exc.code == 429 and attempt < MAX_RETRIES:
                retry_after = int(exc.headers.get("Retry-After", "60"))
                logger.warning("Rate limited for %s. Retrying in %s seconds.", url, retry_after)
                time.sleep(retry_after)
                continue
            raise
        except URLError as exc:
            if attempt == MAX_RETRIES:
                raise
            delay = RETRY_DELAY_SECONDS * (2 ** (attempt - 1)) * random.uniform(0.5, 1.0)
            logger.warning("Fetch failed for %s: %s. Retrying in %.1f seconds.", url, exc, delay)
            time.sleep(delay)

    raise RuntimeError(f"Failed to fetch {url}")


def discover_blog_urls() -> List[str]:
    logger.info("Discovering blog URLs from sitemap: %s", SITEMAP_URL)
    try:
        sitemap = fetch_text(SITEMAP_URL)
        urls = parse_blog_urls_from_sitemap(sitemap)
        if urls:
            logger.info("Discovered %s blog URLs from sitemap.", len(urls))
            return urls
        logger.warning("No blog URLs found in sitemap; falling back to blog index.")
    except Exception as exc:
        logger.warning("Failed to parse sitemap: %s. Falling back to blog index.", exc)

    index_html = fetch_text(BLOG_INDEX_URL)
    urls = parse_blog_urls_from_index(index_html)
    logger.info("Discovered %s blog URLs from blog index fallback.", len(urls))
    return urls


def parse_blog_urls_from_sitemap(sitemap: str) -> List[str]:
    root = ET.fromstring(sitemap)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = []
    locs = root.findall(".//sm:loc", namespace) or root.findall(".//loc")

    for loc in locs:
        if loc.text and is_english_blog_post_url(loc.text):
            urls.append(loc.text.strip())

    return sorted(set(urls), key=slug_from_url)


def parse_blog_urls_from_index(index_html: str) -> List[str]:
    urls = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', index_html):
        absolute = urljoin(BASE_URL, html.unescape(href))
        if is_english_blog_post_url(absolute):
            urls.append(absolute)
    return sorted(set(urls), key=slug_from_url)


def is_english_blog_post_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc != "claude.com":
        return False
    parts = [part for part in parsed.path.split("/") if part]
    return len(parts) == 2 and parts[0] == "blog" and bool(parts[1])


def slug_from_url(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1]


def parse_blog_post(url: str, page_html: str) -> BlogPost:
    json_ld = extract_blog_post_json_ld(page_html)
    slug = slug_from_url(url)

    title = str(json_ld.get("headline") or extract_title(page_html) or slug.replace("-", " ").title())
    description = str(json_ld.get("description") or extract_meta(page_html, "description") or "")
    image = str(json_ld.get("image") or extract_meta(page_html, "og:image") or "")
    date_published_raw = str(json_ld.get("datePublished") or extract_visible_date(page_html) or "")
    date_modified_raw = str(json_ld.get("dateModified") or date_published_raw)

    if not date_published_raw:
        raise ValueError(f"No published date found for {url}")

    date_published = parse_date(date_published_raw)
    body_markdown = extract_body_markdown(page_html)
    validate_body(body_markdown, url)

    return BlogPost(
        url=url,
        slug=slug,
        title=title,
        description=description,
        image=image,
        date_published_raw=date_published_raw,
        date_modified_raw=date_modified_raw,
        date_published=date_published,
        body_markdown=body_markdown,
    )


def extract_blog_post_json_ld(page_html: str) -> Dict[str, object]:
    pattern = re.compile(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(page_html):
        raw = html.unescape(match.group(1)).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            loose_data = parse_loose_blog_post_json_ld(raw)
            if loose_data:
                return loose_data
            continue

        candidates: Iterable[object]
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict) and "@graph" in data and isinstance(data["@graph"], list):
            candidates = data["@graph"]
        else:
            candidates = [data]

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            item_type = candidate.get("@type")
            if item_type == "BlogPosting" or (
                isinstance(item_type, list) and "BlogPosting" in item_type
            ):
                return candidate

    raise ValueError("No BlogPosting JSON-LD found")


def parse_loose_blog_post_json_ld(raw: str) -> Dict[str, object]:
    """Recover key fields from Claude pages with invalid JSON-LD string escaping."""
    if '"@type": "BlogPosting"' not in raw:
        return {}

    recovered: Dict[str, object] = {"@type": "BlogPosting"}
    for key in ("headline", "description", "image", "datePublished", "dateModified"):
        value = extract_loose_json_string(raw, key)
        if value:
            recovered[key] = value
    return recovered if "datePublished" in recovered else {}


def extract_loose_json_string(raw: str, key: str) -> str:
    match = re.search(rf'^\s*"{re.escape(key)}"\s*:\s*"(.*)"[,]?\s*$', raw, re.MULTILINE)
    return html.unescape(match.group(1)).strip() if match else ""


def extract_body_markdown(page_html: str) -> str:
    parser = BlogBodyMarkdownParser()
    parser.feed(page_html)
    parser.close()
    if not parser.found_body:
        raise ValueError("Could not find Claude blog rich-text body")
    return parser.get_markdown()


def extract_title(page_html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", page_html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return html.unescape(re.sub(r"\s+", " ", match.group(1))).replace(" | Claude", "").strip()


def extract_meta(page_html: str, key: str) -> str:
    attr = "property" if key.startswith("og:") else "name"
    pattern = re.compile(
        rf'<meta\s+[^>]*{attr}=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
        re.IGNORECASE,
    )
    match = pattern.search(page_html)
    if match:
        return html.unescape(match.group(1)).strip()

    pattern = re.compile(
        rf'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*{attr}=["\']{re.escape(key)}["\'][^>]*>',
        re.IGNORECASE,
    )
    match = pattern.search(page_html)
    return html.unescape(match.group(1)).strip() if match else ""


def extract_visible_date(page_html: str) -> str:
    match = re.search(
        r">\s*Date\s*</div>\s*<div[^>]*>\s*([^<]+?)\s*</div>",
        page_html,
        re.IGNORECASE | re.DOTALL,
    )
    return html.unescape(match.group(1)).strip() if match else ""


def parse_date(raw_date: str) -> datetime:
    normalized = raw_date.strip()
    normalized = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", normalized)
    normalized = normalized.replace("Sept ", "Sep ")

    for fmt in (
        "%b %d, %Y",
        "%B %d, %Y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue

    raise ValueError(f"Unsupported date format: {raw_date}")


def validate_body(markdown: str, url: str) -> None:
    stripped = markdown.strip()
    if len(stripped) < 100:
        raise ValueError(f"Article body too short for {url} ({len(stripped)} characters)")
    if stripped.startswith("<") or "<html" in stripped[:200].lower():
        raise ValueError(f"Article body looks like raw HTML for {url}")


def render_markdown(post: BlogPost) -> str:
    frontmatter = {
        "title": post.title,
        "source_url": post.url,
        "date": post.date_published.strftime("%Y-%m-%d"),
        "date_published": post.date_published_raw,
        "date_modified": post.date_modified_raw,
        "description": post.description,
        "image": post.image,
    }
    frontmatter_lines = ["---"]
    for key, value in frontmatter.items():
        if value:
            frontmatter_lines.append(f"{key}: {yaml_quote(value)}")
    frontmatter_lines.append("---")

    return "\n".join(frontmatter_lines) + f"\n\n# {post.title}\n\n{post.body_markdown}"


def yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def output_relative_path(post: BlogPost) -> Path:
    year = post.date_published.strftime("%Y")
    month = MONTH_NAMES[post.date_published.month]
    date_prefix = post.date_published.strftime("%Y-%m-%d")
    return Path(year) / month / f"{date_prefix}-{safe_slug(post.slug)}.md"


def safe_slug(slug: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", slug).strip("-._")
    return cleaned or "post"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_manifest(output_dir: Path) -> Dict[str, object]:
    manifest_path = output_dir / MANIFEST_FILE
    if not manifest_path.exists():
        return {"files": {}, "last_updated": None}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data.get("files"), dict):
            data["files"] = {}
        return data
    except Exception as exc:
        logger.warning("Failed to load manifest: %s", exc)
        return {"files": {}, "last_updated": None}


def save_manifest(output_dir: Path, manifest: Dict[str, object]) -> None:
    manifest["last_updated"] = datetime.now().isoformat()
    manifest["description"] = (
        "Claude blog mirror manifest. Markdown files are grouped by published year and month."
    )
    (output_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def save_post(output_dir: Path, relative_path: Path, content: str) -> None:
    full_path = output_dir / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")


def cleanup_old_files(output_dir: Path, manifest: Dict[str, object], current_files: Set[str]) -> None:
    previous = set(manifest.get("files", {}).keys()) if isinstance(manifest.get("files"), dict) else set()
    for relative in sorted(previous - current_files):
        path = output_dir / relative
        if path.suffix != ".md":
            continue
        try:
            path.relative_to(output_dir)
        except ValueError:
            continue
        if path.exists():
            logger.info("Removing obsolete mirrored file: %s", relative)
            path.unlink()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mirror Claude blog posts as Markdown.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "blog",
        help="Directory where Markdown files and the manifest are written.",
    )
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="Fetch only the specified Claude blog article URL. Can be used multiple times.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit the number of discovered posts to fetch. Useful for testing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse posts without writing files.",
    )
    parser.add_argument(
        "--keep-obsolete",
        action="store_true",
        help="Do not remove files that were present in the previous manifest but not in this run.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    start_time = datetime.now()
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(output_dir)

    urls = [normalize_blog_url(url) for url in args.url] if args.url else discover_blog_urls()
    if args.limit:
        urls = urls[: args.limit]
    if not urls:
        logger.error("No blog URLs to fetch.")
        return 1

    old_files = manifest.get("files", {}) if isinstance(manifest.get("files"), dict) else {}
    is_partial_run = bool(args.url or args.limit)
    new_manifest: Dict[str, object] = {"files": dict(old_files) if is_partial_run else {}}
    current_files: Set[str] = set()
    successful = 0
    failed = 0
    failed_urls: List[str] = []

    for index, url in enumerate(urls, start=1):
        logger.info("Processing %s/%s: %s", index, len(urls), url)
        try:
            page_html = fetch_text(url)
            post = parse_blog_post(url, page_html)
            markdown = render_markdown(post)
            content_hash = sha256_text(markdown)
            relative_path = output_relative_path(post)
            relative_key = relative_path.as_posix()
            previous_entry = old_files.get(relative_key, {}) if isinstance(old_files, dict) else {}
            previous_hash = previous_entry.get("hash", "") if isinstance(previous_entry, dict) else ""

            if not args.dry_run and content_hash != previous_hash:
                save_post(output_dir, relative_path, markdown)
                logger.info("Saved: %s", relative_key)
            elif content_hash == previous_hash:
                logger.info("Unchanged: %s", relative_key)
            else:
                logger.info("Dry run parsed: %s", relative_key)

            last_updated = (
                previous_entry.get("last_updated")
                if isinstance(previous_entry, dict) and content_hash == previous_hash
                else datetime.now().isoformat()
            )
            new_manifest["files"][relative_key] = {
                "title": post.title,
                "original_url": post.url,
                "hash": content_hash,
                "date": post.date_published.strftime("%Y-%m-%d"),
                "date_published": post.date_published_raw,
                "date_modified": post.date_modified_raw,
                "last_updated": last_updated,
            }
            current_files.add(relative_key)
            successful += 1
        except Exception as exc:
            logger.error("Failed to process %s: %s", url, exc)
            failed += 1
            failed_urls.append(url)

        if index < len(urls):
            time.sleep(RATE_LIMIT_SECONDS)

    if not args.keep_obsolete and not args.url and not args.limit and not args.dry_run:
        cleanup_old_files(output_dir, manifest, current_files)

    new_manifest["fetch_metadata"] = {
        "last_fetch_completed": datetime.now().isoformat(),
        "fetch_duration_seconds": (datetime.now() - start_time).total_seconds(),
        "total_pages_discovered": len(urls),
        "pages_fetched_successfully": successful,
        "pages_failed": failed,
        "failed_urls": failed_urls,
        "sitemap_url": SITEMAP_URL,
        "base_url": BASE_URL,
        "fetch_tool_version": "1.0",
    }

    if not args.dry_run:
        save_manifest(output_dir, new_manifest)

    logger.info("Fetch completed: %s successful, %s failed.", successful, failed)
    if failed and successful == 0:
        return 1
    return 0


def normalize_blog_url(url: str) -> str:
    absolute = urljoin(BASE_URL, url)
    if not is_english_blog_post_url(absolute):
        raise ValueError(f"Not an English Claude blog article URL: {url}")
    return absolute


if __name__ == "__main__":
    sys.exit(main())
