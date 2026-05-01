#!/usr/bin/env python3
"""
Mirror Claude Developer Platform docs as Markdown files.

The platform docs already serve Markdown at their canonical URLs, so this
script discovers English docs pages from the sitemap and saves them using the
same hierarchy as /docs/en/<path>.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://platform.claude.com"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
DOCS_PREFIX = "/docs/en/"
MANIFEST_FILE = "docs_manifest.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 Claude-Platform-Docs-Mirror/1.0"
    ),
    "Accept": "text/markdown,text/plain,text/html;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0
RATE_LIMIT_SECONDS = 0.25


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class NonMarkdownPageError(ValueError):
    """Raised when a sitemap URL does not serve Markdown content."""


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


def discover_docs_urls() -> List[str]:
    logger.info("Discovering docs URLs from sitemap: %s", SITEMAP_URL)
    sitemap = fetch_text(SITEMAP_URL)
    root = ET.fromstring(sitemap)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = root.findall(".//sm:loc", namespace) or root.findall(".//loc")

    urls = []
    for loc in locs:
        if loc.text and is_english_docs_url(loc.text):
            urls.append(loc.text.strip())

    urls = sorted(set(urls), key=url_to_sort_key)
    logger.info("Discovered %s English docs URLs.", len(urls))
    return urls


def is_english_docs_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc == "platform.claude.com" and parsed.path.startswith(DOCS_PREFIX)


def normalize_docs_url(url: str) -> str:
    absolute = urljoin(BASE_URL, url)
    if absolute.endswith(".md"):
        absolute = absolute[:-3]
    if not is_english_docs_url(absolute):
        raise ValueError(f"Not an English Claude docs URL: {url}")
    return absolute


def url_to_sort_key(url: str) -> str:
    return urlparse(url).path.removeprefix(DOCS_PREFIX)


def url_to_relative_path(url: str) -> Path:
    relative = urlparse(url).path.removeprefix(DOCS_PREFIX).strip("/")
    if not relative:
        relative = "index"

    segments = [safe_path_segment(segment) for segment in relative.split("/") if segment]
    if not segments:
        segments = ["index"]

    filename = segments[-1]
    if not filename.endswith(".md"):
        filename += ".md"
    return Path(*segments[:-1]) / filename


def safe_path_segment(segment: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", segment).strip("-._")
    return cleaned or "page"


def validate_markdown(content: str, url: str) -> None:
    stripped = content.strip()
    if stripped.startswith("<!DOCTYPE") or "<html" in stripped[:200].lower():
        raise NonMarkdownPageError(f"Received HTML instead of Markdown for {url}")
    if len(stripped) < 20 and not stripped.startswith("#"):
        raise ValueError(f"Content too short for {url} ({len(stripped)} characters)")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_manifest(output_dir: Path) -> Dict[str, object]:
    path = output_dir / MANIFEST_FILE
    if not path.exists():
        return {"files": {}, "last_updated": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("files"), dict):
            data["files"] = {}
        return data
    except Exception as exc:
        logger.warning("Failed to load manifest: %s", exc)
        return {"files": {}, "last_updated": None}


def save_manifest(output_dir: Path, manifest: Dict[str, object]) -> None:
    manifest["last_updated"] = datetime.now().isoformat()
    manifest["description"] = (
        "Claude Developer Platform docs mirror manifest. Keys are Markdown paths under docs/."
    )
    (output_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def save_markdown(output_dir: Path, relative_path: Path, content: str) -> None:
    path = output_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def cleanup_old_files(output_dir: Path, manifest: Dict[str, object], current_files: Set[str]) -> None:
    files = manifest.get("files", {})
    previous_files = set(files.keys()) if isinstance(files, dict) else set()
    for relative in sorted(previous_files - current_files):
        path = output_dir / relative
        if path.suffix != ".md":
            continue
        if path.exists():
            logger.info("Removing obsolete mirrored file: %s", relative)
            path.unlink()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mirror Claude Developer Platform docs as Markdown.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "docs",
        help="Directory where Markdown files and the manifest are written.",
    )
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="Fetch only the specified docs URL. Can be used multiple times.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit the number of discovered docs pages to fetch. Useful for testing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and validate docs without writing files.",
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
    old_files = manifest.get("files", {}) if isinstance(manifest.get("files"), dict) else {}
    is_partial_run = bool(args.url or args.limit)
    new_manifest: Dict[str, object] = {"files": dict(old_files) if is_partial_run else {}}
    current_files: Set[str] = set()
    failed_urls: List[str] = []
    skipped_urls: List[str] = []

    urls = [normalize_docs_url(url) for url in args.url] if args.url else discover_docs_urls()
    if args.limit:
        urls = urls[: args.limit]
    if not urls:
        logger.error("No docs URLs to fetch.")
        return 1

    successful = 0
    failed = 0

    for index, url in enumerate(urls, start=1):
        logger.info("Processing %s/%s: %s", index, len(urls), url)
        try:
            relative_path = url_to_relative_path(url)
            relative_key = relative_path.as_posix()
            previous_entry = old_files.get(relative_key, {}) if isinstance(old_files, dict) else {}

            content = fetch_text(url)
            try:
                validate_markdown(content, url)
            except NonMarkdownPageError as exc:
                logger.warning("Skipping non-Markdown docs page: %s", url)
                if isinstance(previous_entry, dict) and previous_entry:
                    new_manifest["files"][relative_key] = previous_entry
                    current_files.add(relative_key)
                skipped_urls.append(url)
                continue

            new_hash = content_hash(content)
            previous_hash = previous_entry.get("hash", "") if isinstance(previous_entry, dict) else ""

            if args.dry_run:
                logger.info("Dry run parsed: %s", relative_key)
            elif new_hash != previous_hash:
                save_markdown(output_dir, relative_path, content)
                logger.info("Saved: %s", relative_key)
            else:
                logger.info("Unchanged: %s", relative_key)

            last_updated = (
                previous_entry.get("last_updated")
                if isinstance(previous_entry, dict) and new_hash == previous_hash
                else datetime.now().isoformat()
            )
            new_manifest["files"][relative_key] = {
                "original_url": url,
                "hash": new_hash,
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

    if not args.keep_obsolete and not is_partial_run and not args.dry_run:
        cleanup_old_files(output_dir, manifest, current_files)

    fetch_metadata = {
        "last_fetch_completed": datetime.now().isoformat(),
        "fetch_duration_seconds": (datetime.now() - start_time).total_seconds(),
        "total_pages_discovered": len(urls),
        "pages_fetched_successfully": successful,
        "pages_failed": failed,
        "pages_skipped_non_markdown": len(skipped_urls),
        "failed_urls": failed_urls,
        "skipped_urls": skipped_urls,
        "sitemap_url": SITEMAP_URL,
        "base_url": BASE_URL,
        "fetch_tool_version": "1.0",
    }
    if is_partial_run and isinstance(manifest.get("fetch_metadata"), dict):
        new_manifest["fetch_metadata"] = manifest["fetch_metadata"]
        new_manifest["last_partial_fetch_metadata"] = fetch_metadata
    else:
        new_manifest["fetch_metadata"] = fetch_metadata

    if not args.dry_run:
        save_manifest(output_dir, new_manifest)

    logger.info(
        "Fetch completed: %s successful, %s skipped, %s failed.",
        successful,
        len(skipped_urls),
        failed,
    )
    if failed and successful == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
