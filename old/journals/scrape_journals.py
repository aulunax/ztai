#!/usr/bin/env python3
"""Simplified scraper for OJS journals.umcs.pl pages."""

import argparse
import concurrent.futures as cf
import csv
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urljoin, urlparse
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger("journals_scraper")
ARTICLE_VIEW_RE = re.compile(r"/article/view/(\d+)(?:/([^/?#]+))?")

@dataclass
class ArticleRecord:
    article_url: str
    journal_name: str | None
    journal_name_with_issue: str | None
    pdf_download_url: str | None
    language: str  # <-- Added language field

class JournalsScraper:
    def __init__(self, delay: float = 0.0, timeout: int = 20):
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        retries = Retry(total=4, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; JournalsScraper/2.0)",
                                     "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",})

    def _get_soup(self, url: str) -> BeautifulSoup | None:
        try:
            LOGGER.debug("GET %s", url)
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            LOGGER.warning("Failed to fetch %s: %s", url, e)
            return None

    def extract_issue_links(self, archive_url: str) -> list[str]:
        """Crawls pagination to find all issue links."""
        issue_links, visited, queue = [], set(), [archive_url]

        while queue:
            page_url = queue.pop(0).split("#")[0]
            if page_url in visited: continue
            
            visited.add(page_url)
            soup = self._get_soup(page_url)
            if not soup: continue

            # Extract issues
            issues = [urljoin(page_url, a.get("href", "")) for a in soup.select("a[href*='/issue/view/']")]
            issue_links.extend([url for url in issues if "/issue/view/" in url])

            # Pagination (Next pages)
            for a in soup.select("a[href*='/issue/archive']"):
                next_url = urljoin(page_url, a.get("href", ""))
                if "user/setLocale" not in next_url and next_url not in visited:
                    queue.append(next_url)

        deduped = list(dict.fromkeys(issue_links))
        LOGGER.info("Found %d issue links.", len(deduped))
        return deduped

    def extract_article_entries(self, issue_url: str) -> list[dict]:
        """Finds article URLs, PDF view links, and Language from an issue TOC."""
        issue_soup = self._get_soup(issue_url)
        if not issue_soup: return []

        # Get Issue Display Name
        title_tag = issue_soup.select_one("h1, h3, .page_title, title")
        issue_name = title_tag.get_text(" ", strip=True) if title_tag else None

        # Go to TOC
        toc_a = issue_soup.select_one("a[href*='/showToc']")
        toc_url = urljoin(issue_url, toc_a["href"]) if toc_a else f"{issue_url.rstrip('/')}/showToc"
        toc_soup = self._get_soup(toc_url) or issue_soup

        # Map Articles to their PDF views and Language
        articles = {}
        for a in toc_soup.select("a[href*='/article/view/']"):
            href = urljoin(toc_url, a.get("href", ""))
            match = ARTICLE_VIEW_RE.search(href)
            if not match: continue
            
            # Construct canonical URL
            article_id = match.group(1)
            base_href = href.split(f"/article/view/{article_id}")[0]
            canon_url = f"{base_href}/article/view/{article_id}"
            
            if canon_url not in articles:
                articles[canon_url] = {"pdf_url": None, "language": "Polish"}
                
            # Check if this link is the PDF link (by suffix or by HTML class)
            suffix = str(match.group(2)).lower()
            if suffix == "pdf" or "file" in a.get("class", []):
                articles[canon_url]["pdf_url"] = href
                
                # Language extraction logic based on HTML snippet
                if "(ENGLISH)" in a.get_text().upper():
                    articles[canon_url]["language"] = "English"

        # Ensure fallback PDF url
        for url, data in articles.items():
            if not data["pdf_url"]: data["pdf_url"] = f"{url}/pdf"

        return [
            {
                "article_url": u, 
                "pdf_view_url": d["pdf_url"], 
                "issue_name": issue_name,
                "language": d["language"]
            } for u, d in articles.items()
        ]

    def extract_article_record(self, entry: dict) -> ArticleRecord | None:
        """Visits article/PDF page to extract metadata."""
        target_url = entry["pdf_view_url"] or f"{entry['article_url']}/pdf"
        soup = self._get_soup(target_url)
        if not soup: return None

        # Journal Name
        meta_j = soup.find("meta", attrs={"name": lambda x: x and x.lower() in ["citation_journal_title", "dc.source"]})
        journal_name = meta_j["content"].strip() if meta_j else None
        
        issue_name = entry["issue_name"]
        j_issue = f"{journal_name} - {issue_name}" if journal_name and issue_name else (issue_name or journal_name)

        # PDF Link
        pdf_link = None
        meta_pdf = soup.find("meta", attrs={"name": "citation_pdf_url"})
        if meta_pdf:
            pdf_link = urljoin(entry["article_url"], meta_pdf["content"])
        else:
            download_a = soup.select_one("a[href*='/article/download/']")
            if download_a:
                pdf_link = urljoin(entry["article_url"], download_a.get("href", ""))
            elif entry["pdf_view_url"]:
                pdf_link = entry["pdf_view_url"].replace("/view/", "/download/")

        return ArticleRecord(
            article_url=entry["article_url"], 
            journal_name=journal_name, 
            journal_name_with_issue=j_issue, 
            pdf_download_url=pdf_link,
            language=entry["language"] # <-- Map language to final record
        )

def write_output(records: list, output: str, fmt: str, pretty: bool):
    data = [asdict(r) for r in records if r]
    if fmt == "json":
        out_str = json.dumps(data, ensure_ascii=False, indent=2 if pretty else None)
        if output: Path(output).write_text(out_str, encoding="utf-8")
        else: print(out_str)
    else:
        file_obj = open(output, "w", encoding="utf-8", newline="") if output else sys.stdout
        writer = csv.DictWriter(file_obj, fieldnames=data[0].keys() if data else [])
        writer.writeheader()
        writer.writerows(data)
        if output: file_obj.close()
    LOGGER.info("Saved %d records.", len(data))

def main():
    parser = argparse.ArgumentParser(description="Simplified OJS Scraper")
    parser.add_argument("--archive-url", default="https://journals.umcs.pl/sil/issue/archive")
    parser.add_argument("--max-issues", type=int)
    parser.add_argument("--max-articles", type=int)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--output")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    scraper = JournalsScraper()

    # 1. Get Issues
    issue_urls = scraper.extract_issue_links(args.archive_url)
    if args.max_issues: issue_urls = issue_urls[:args.max_issues]

    # 2. Get Article Links
    entries = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(scraper.extract_article_entries, issue_urls):
            entries.extend(res)
    
    entries = list({e["article_url"]: e for e in entries}.values()) # Dedupe by URL
    if args.max_articles: entries = entries[:args.max_articles]
    LOGGER.info("Found %d unique articles.", len(entries))

    # 3. Get Metadata
    records = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        records = list(ex.map(scraper.extract_article_record, entries))

    write_output(records, args.output, args.format, args.pretty)

if __name__ == "__main__":
    main()