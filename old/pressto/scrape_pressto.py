#!/usr/bin/env python3
"""Scrape PRESSto/OJS pages for article URL, journal name, and PDF link."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import logging
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


USER_AGENT = "Mozilla/5.0 (compatible; PresstoScraper/1.0; +https://pressto.amu.edu.pl)"
ISSUE_LINK_RE = re.compile(r"/issue/view/\d+")
ARTICLE_VIEW_RE = re.compile(r"/article/view/(\d+)(?:/(\d+))?")
ARCHIVE_PAGE_RE = re.compile(r"/issue/archive(?:/\d+)?/?$")
LOGGER = logging.getLogger("pressto_scraper")


@dataclass
class ArticleRecord:
    article_url: str
    journal_name: str | None
    journal_name_with_issue: str | None
    pdf_download_url: str | None


@dataclass
class ArticleEntry:
    article_url: str
    issue_display_name: str | None


class PresstoScraper:
    def __init__(self, delay: float = 0.0, timeout: int = 20) -> None:
        self.delay = max(delay, 0.0)
        self.timeout = timeout
        self._thread_local = threading.local()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"User-Agent": USER_AGENT})
        return session

    def _fetch_soup(self, url: str) -> BeautifulSoup:
        LOGGER.info("GET %s", url)
        response = self._get_session().get(url, timeout=self.timeout)
        response.raise_for_status()
        if self.delay > 0:
            time.sleep(self.delay)
        return BeautifulSoup(response.text, "html.parser")

    def _get_session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._build_session()
            self._thread_local.session = session
        return session

    @staticmethod
    def _dedupe_keep_order(items: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        output: list[str] = []
        for item in items:
            if item and item not in seen:
                seen.add(item)
                output.append(item)
        return output

    @staticmethod
    def _is_archive_page_url(url: str) -> bool:
        return bool(ARCHIVE_PAGE_RE.search(urlparse(url).path))

    @staticmethod
    def _canonical_article_url(url: str) -> str:
        match = ARTICLE_VIEW_RE.search(url)
        if not match:
            return url

        article_id = match.group(1)
        parsed = urlparse(url)
        canonical_path = re.sub(r"/article/view/\d+(?:/\d+)?", f"/article/view/{article_id}", parsed.path)
        return parsed._replace(path=canonical_path, query="", fragment="").geturl()

    @staticmethod
    def _view_to_download_url(url: str) -> str | None:
        match = ARTICLE_VIEW_RE.search(url)
        if not match:
            return None

        article_id = match.group(1)
        galley_id = match.group(2)
        if not galley_id:
            return None

        parsed = urlparse(url)
        download_path = re.sub(
            r"/article/view/\d+/\d+",
            f"/article/download/{article_id}/{galley_id}",
            parsed.path,
        )
        return parsed._replace(path=download_path, query="", fragment="").geturl()

    def _extract_archive_page_links(self, soup: BeautifulSoup, page_url: str) -> list[str]:
        links: list[str] = []
        for a_tag in soup.select("a[href*='/issue/archive']"):
            href = a_tag.get("href", "").strip()
            if not href:
                continue
            absolute = urljoin(page_url, href)
            if self._is_archive_page_url(absolute):
                links.append(absolute)
        return self._dedupe_keep_order(links)

    def extract_issue_links(self, archive_url: str) -> list[str]:
        issue_links: list[str] = []
        visited_pages: set[str] = set()
        pages_to_visit: list[str] = [archive_url]

        while pages_to_visit:
            page_url = pages_to_visit.pop(0)
            if page_url in visited_pages:
                continue

            visited_pages.add(page_url)
            soup = self._fetch_soup(page_url)

            page_issue_links: list[str] = []
            for a_tag in soup.select("a[href*='/issue/view/']"):
                href = a_tag.get("href", "").strip()
                if not href:
                    continue
                absolute = urljoin(page_url, href)
                if ISSUE_LINK_RE.search(absolute):
                    page_issue_links.append(absolute)

            deduped_page_issue_links = self._dedupe_keep_order(page_issue_links)
            issue_links.extend(deduped_page_issue_links)
            LOGGER.info("Found %d issue links on archive page: %s", len(deduped_page_issue_links), page_url)

            for archive_page_link in self._extract_archive_page_links(soup, page_url):
                if archive_page_link not in visited_pages and archive_page_link not in pages_to_visit:
                    pages_to_visit.append(archive_page_link)

        deduped = self._dedupe_keep_order(issue_links)
        LOGGER.info(
            "Found %d issue links across %d archive pages, starting from: %s",
            len(deduped),
            len(visited_pages),
            archive_url,
        )
        return deduped

    def _extract_issue_display_name(self, issue_soup: BeautifulSoup) -> str | None:
        for selector in ("h1.page_title", "h1.page-header", "h1"):
            tag = issue_soup.select_one(selector)
            if tag:
                text = tag.get_text(" ", strip=True)
                if text:
                    return text

        title_tag = issue_soup.find("title")
        if title_tag:
            title_text = title_tag.get_text(" ", strip=True)
            if title_text:
                return title_text

        return None

    def extract_article_entries(self, issue_url: str) -> list[ArticleEntry]:
        soup = self._fetch_soup(issue_url)
        article_links: list[str] = []
        issue_display_name = self._extract_issue_display_name(soup)

        for a_tag in soup.select(".article-summary-title a[href*='/article/view/']"):
            href = a_tag.get("href", "").strip()
            if not href:
                continue
            absolute = urljoin(issue_url, href)
            article_links.append(self._canonical_article_url(absolute))

        # Fallback for theme changes.
        if not article_links:
            for a_tag in soup.select("a[href*='/article/view/']"):
                href = a_tag.get("href", "").strip()
                if not href:
                    continue
                absolute = urljoin(issue_url, href)
                if ARTICLE_VIEW_RE.search(absolute):
                    article_links.append(self._canonical_article_url(absolute))

        deduped = self._dedupe_keep_order(article_links)
        LOGGER.info("Found %d article links in issue: %s", len(deduped), issue_url)
        return [ArticleEntry(article_url=url, issue_display_name=issue_display_name) for url in deduped]

    @staticmethod
    def _meta_content(soup: BeautifulSoup, *names: str) -> str | None:
        for name in names:
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                value = tag["content"].strip()
                if value:
                    return value
        return None

    @staticmethod
    def _meta_property_content(soup: BeautifulSoup, *properties: str) -> str | None:
        for prop in properties:
            tag = soup.find("meta", attrs={"property": prop})
            if tag and tag.get("content"):
                value = tag["content"].strip()
                if value:
                    return value
        return None

    def _extract_journal_name(self, soup: BeautifulSoup) -> str | None:
        journal = self._meta_content(
            soup,
            "citation_journal_title",
            "DC.Source",
            "dc.source",
            "description",
        )
        if journal:
            return journal

        journal_prop = self._meta_property_content(soup, "og:site_name")
        if journal_prop:
            return journal_prop

        title_tag = soup.find("title")
        if title_tag:
            title_text = title_tag.get_text(" ", strip=True)
            if "|" in title_text:
                return title_text.split("|")[-1].strip() or None

        return None

    def _extract_pdf_link(self, soup: BeautifulSoup, article_url: str) -> str | None:
        meta_pdf = self._meta_content(soup, "citation_pdf_url")
        if meta_pdf:
            return urljoin(article_url, meta_pdf)

        article_match = ARTICLE_VIEW_RE.search(article_url)
        article_id = article_match.group(1) if article_match else None

        if article_id:
            direct_selector = f"a[href*='/article/download/{article_id}/']"
            direct_link = soup.select_one(direct_selector)
            if direct_link and direct_link.get("href"):
                return urljoin(article_url, direct_link.get("href", "").strip())

            # On PRESSto article pages, galleys are often /article/view/<id>/<galley>.
            view_selector = f"a[href*='/article/view/{article_id}/']"
            for link in soup.select(view_selector):
                href = link.get("href", "").strip()
                if not href:
                    continue
                absolute = urljoin(article_url, href)
                converted = self._view_to_download_url(absolute)
                if converted:
                    return converted

        # Generic fallback: first download link on the page.
        any_download = soup.select_one("a[href*='/article/download/']")
        if any_download and any_download.get("href"):
            return urljoin(article_url, any_download.get("href", "").strip())

        return None

    @staticmethod
    def _compose_journal_name_with_issue(journal_name: str | None, issue_display_name: str | None) -> str | None:
        if issue_display_name:
            if journal_name and issue_display_name.lower().startswith(journal_name.lower()):
                return issue_display_name
            if journal_name:
                return f"{journal_name} - {issue_display_name}"
            return issue_display_name
        return journal_name

    def extract_article_record(self, article_url: str, issue_display_name: str | None = None) -> ArticleRecord:
        LOGGER.info("Extracting article record: %s", article_url)
        soup = self._fetch_soup(article_url)

        journal_name = self._extract_journal_name(soup)
        journal_name_with_issue = self._compose_journal_name_with_issue(journal_name, issue_display_name)
        pdf_link = self._extract_pdf_link(soup, article_url)

        return ArticleRecord(
            article_url=article_url,
            journal_name=journal_name,
            journal_name_with_issue=journal_name_with_issue,
            pdf_download_url=pdf_link,
        )


def write_output(records: list[ArticleRecord], output_path: str | None, output_format: str, pretty: bool) -> None:
    if output_format == "json":
        payload = [asdict(record) for record in records]
        json_text = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
        if output_path:
            with open(output_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(json_text)
            LOGGER.info("Wrote %d records to %s", len(records), output_path)
        else:
            print(json_text)
            LOGGER.info("Printed %d records to stdout", len(records))
        return

    fieldnames = ["article_url", "journal_name", "journal_name_with_issue", "pdf_download_url"]
    if output_path:
        with open(output_path, "w", encoding="utf-8", newline="") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
            writer.writeheader()
            for record in records:
                writer.writerow(asdict(record))
        LOGGER.info("Wrote %d records to %s", len(records), output_path)
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))
        LOGGER.info("Printed %d records to stdout", len(records))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape PRESSto pages and return article URL, journal name, and original PDF link."
    )
    parser.add_argument(
        "--archive-url",
        default="https://pressto.amu.edu.pl/index.php/rpeis/issue/archive",
        help="Archive URL of a journal (used when --issue-url is not provided).",
    )
    parser.add_argument(
        "--issue-url",
        action="append",
        default=[],
        help="Issue page URL. Can be provided multiple times.",
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=None,
        help="Optional limit for number of issues to scrape.",
    )
    parser.add_argument(
        "--max-articles",
        type=int,
        default=None,
        help="Optional limit for number of articles to scrape.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Delay between requests in seconds.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Number of parallel worker threads for issue/article requests.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="Output format.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path. If omitted, prints to stdout.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    LOGGER.info("Starting PRESSto scraper")

    scraper = PresstoScraper(delay=args.delay, timeout=args.timeout)

    issue_urls = args.issue_url if args.issue_url else scraper.extract_issue_links(args.archive_url)
    if args.max_issues is not None:
        issue_urls = issue_urls[: max(args.max_issues, 0)]
    LOGGER.info("Using %d issue URLs", len(issue_urls))

    workers = max(args.workers, 1)

    all_entries: list[ArticleEntry] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_issue = {executor.submit(scraper.extract_article_entries, issue_url): issue_url for issue_url in issue_urls}
        for future in cf.as_completed(future_to_issue):
            issue_url = future_to_issue[future]
            try:
                all_entries.extend(future.result())
            except Exception as exc:  # noqa: BLE001
                print(f"Warning: failed to scrape issue {issue_url}: {exc}", file=sys.stderr)

    deduped_entries: list[ArticleEntry] = []
    seen_article_urls: set[str] = set()
    for entry in all_entries:
        if entry.article_url in seen_article_urls:
            continue
        seen_article_urls.add(entry.article_url)
        deduped_entries.append(entry)

    if args.max_articles is not None:
        deduped_entries = deduped_entries[: max(args.max_articles, 0)]
    LOGGER.info("Collected %d unique article URLs", len(deduped_entries))

    records_by_index: list[ArticleRecord | None] = [None] * len(deduped_entries)
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_entry = {
            executor.submit(scraper.extract_article_record, entry.article_url, entry.issue_display_name): (index, entry)
            for index, entry in enumerate(deduped_entries)
        }
        for future in cf.as_completed(future_to_entry):
            index, entry = future_to_entry[future]
            try:
                records_by_index[index] = future.result()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Failed to scrape article %s: %s", entry.article_url, exc)

    records = [record for record in records_by_index if record is not None]

    LOGGER.info("Finished scraping. Successful records: %d", len(records))
    write_output(records, args.output, args.format, args.pretty)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
