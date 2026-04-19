#!/usr/bin/env python3
"""Scrape OJS journal pages for article URL, journal name, and PDF link."""

from __future__ import annotations

import argparse
from collections import Counter
import concurrent.futures as cf
import csv
import json
import logging
from pathlib import Path
import re
import sys
import threading
import time
from dataclasses import dataclass, asdict
from io import BytesIO
from statistics import median
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


USER_AGENT = "Mozilla/5.0 (compatible; CzasopismaScraper/1.0; +https://czasopisma.uksw.edu.pl)"
ISSUE_LINK_RE = re.compile(r"/issue/view/\d+")
ARTICLE_VIEW_RE = re.compile(r"/article/view/(\d+)(?:/(\d+))?")
ARCHIVE_PAGE_RE = re.compile(r"/issue/archive(?:/\d+)?/?$")
LOGGER = logging.getLogger("czasopisma_scraper")


@dataclass
class ArticleRecord:
    article_url: str
    journal_name: str | None
    journal_name_with_issue: str | None
    article_language: str
    licence_info: str | None
    pdf_download_url: str | None
    article_text: str | None


@dataclass
class ArticleEntry:
    article_url: str
    issue_display_name: str | None


@dataclass
class PdfLine:
    text: str
    page_no: int
    top: float
    bottom: float
    x0: float
    x1: float
    size: float
    page_width: float
    page_height: float

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def is_centered(self) -> bool:
        return abs(self.center_x - self.page_width / 2.0) < self.page_width * 0.16


def _extract_pdf_page_lines(page: object, page_no: int) -> list[PdfLine]:
    words = page.extract_words(
        use_text_flow=True,
        keep_blank_chars=False,
        x_tolerance=2,
        y_tolerance=3,
        extra_attrs=["size"],
    )
    if not words:
        return []

    words = sorted(words, key=lambda w: (float(w["top"]), float(w["x0"])))
    raw_lines: list[dict[str, object]] = []
    y_tol = 2.8

    for word in words:
        text = str(word.get("text", "")).strip()
        if not text:
            continue

        top = float(word["top"])
        bottom = float(word["bottom"])
        x0 = float(word["x0"])
        x1 = float(word["x1"])
        size = float(word.get("size", 0) or 0)

        if not raw_lines or abs(top - float(raw_lines[-1]["top"])) > y_tol:
            raw_lines.append(
                {
                    "top": top,
                    "bottom": bottom,
                    "x0": x0,
                    "x1": x1,
                    "parts": [text],
                    "sizes": [size],
                    "tokens": [{"text": text, "size": size, "top": top}],
                }
            )
            continue

        line = raw_lines[-1]
        line["top"] = min(float(line["top"]), top)
        line["bottom"] = max(float(line["bottom"]), bottom)
        line["x0"] = min(float(line["x0"]), x0)
        line["x1"] = max(float(line["x1"]), x1)
        line["parts"].append(text)
        line["sizes"].append(size)
        line["tokens"].append({"text": text, "size": size, "top": top})

    out: list[PdfLine] = []
    for line in raw_lines:
        tokens = list(line.get("tokens", []))

        if tokens:
            numeric_re = re.compile(r"^\d{1,3}$")
            non_numeric_sizes = [
                float(tok["size"])
                for tok in tokens
                if not numeric_re.match(str(tok["text"])) and float(tok["size"]) > 0
            ]
            all_sizes = [float(tok["size"]) for tok in tokens if float(tok["size"]) > 0]
            base_size = median(non_numeric_sizes) if non_numeric_sizes else (median(all_sizes) if all_sizes else 0.0)

            non_numeric_tops = [float(tok["top"]) for tok in tokens if not numeric_re.match(str(tok["text"]))]
            all_tops = [float(tok["top"]) for tok in tokens]
            base_top = median(non_numeric_tops) if non_numeric_tops else (median(all_tops) if all_tops else 0.0)

            filtered_tokens: list[dict[str, object]] = []
            for idx, tok in enumerate(tokens):
                token_text = str(tok["text"]).strip()
                token_size = float(tok["size"])
                token_top = float(tok["top"])

                if numeric_re.match(token_text):
                    tiny = token_size <= base_size - 1.4
                    raised = token_top <= base_top - 0.6
                    edge = idx == 0 or idx == len(tokens) - 1
                    if tiny and (raised or edge):
                        continue

                filtered_tokens.append(tok)

            tokens = filtered_tokens

        text = re.sub(r"\s+", " ", " ".join(str(tok["text"]) for tok in tokens).strip())
        if not text:
            continue
        sizes = [v for v in line["sizes"] if v > 0]
        out.append(
            PdfLine(
                text=text,
                page_no=page_no,
                top=float(line["top"]),
                bottom=float(line["bottom"]),
                x0=float(line["x0"]),
                x1=float(line["x1"]),
                size=float(median(sizes) if sizes else 0.0),
                page_width=float(page.width),
                page_height=float(page.height),
            )
        )
    return out


def _is_running_header_or_footer(line: PdfLine, body_size: float) -> bool:
    near_top = line.top < line.page_height * 0.12
    near_bottom = line.top > line.page_height * 0.90
    small = line.size <= body_size - 1.8
    header_signature = bool(re.search(r"\[\d+\]", line.text) or re.search(r"\b\d+\b", line.text[:24]))

    if near_bottom and small:
        return True
    if near_top and (small or header_signature):
        return True
    return False


def _is_bottom_annotation(line: PdfLine, body_size: float) -> bool:
    if line.top < line.page_height * 0.45:
        return False
    if line.size > body_size - 0.9:
        return False
    if re.match(r"^\d+\s+", line.text):
        return True
    if re.search(r"\b(?:Dz\.\s*U\.|OSNC|Legalis|Lex|Glosa|Monitor|Przeglad)\b", line.text):
        return True
    return True

def _is_back_matter_anchor(text: str) -> bool:
    return bool(
        re.search(
            r"\b(Streszczenie|Summary|S\w*owa\s+kluczowe|Keywords|Literatura|Bibliografia|Data\s+wp\w+yni\w+cia|Data\s+zaakceptowania|Conflict\s+of\s+interest|Finansowanie\s+bada\w+|A\s+Member\s+of\s+a\s+Company(?:’|')?s\s+Management\s+Board)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _is_front_matter_meta_line(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return True

    return bool(
        re.search(
            r"(?:\b(?:e?ISSN|ORCID|Creative\s+Commons|Libre\s+Open\s+Access|doi\.org|https?://doi\.org|e-?mail|email)\b|"
            r"Artyku\w*\s+zosta\w*\s+opublikowan\w*|"
            r"obj\w*ty\s+warunkami\s+licencji|"
            r"\b26\.\d+\s*/\s*\d{4},\s*s\.\s*\d+|"
            r"\b(?:Data\s+wp\w+yni\w+cia|Data\s+zaakceptowania|Received|Accepted)\b)",
            normalized,
            flags=re.IGNORECASE,
        )
    )


def _is_body_paragraph_line(line: PdfLine, body_size: float) -> bool:
    text = line.text.strip()
    if len(text) < 35:
        return False
    if not re.search(r"[a-ząćęłńóśźż]", text, flags=re.IGNORECASE):
        return False
    if _is_front_matter_meta_line(text):
        return False
    return body_size - 0.3 <= line.size <= body_size + 0.6


def _is_title_block_line(line: PdfLine, body_size: float) -> bool:
    text = line.text.strip()
    if not text:
        return False
    if _is_front_matter_meta_line(text):
        return False

    alpha = sum(1 for char in text if char.isalpha())
    upper = sum(1 for char in text if char.isupper())
    uppercase_ratio = upper / max(1, alpha)

    return bool(
        re.match(r"^\d+\.\s+\S", text)
        or line.size >= body_size + 0.7
        or (line.is_centered and line.size >= body_size + 0.3 and len(text) <= 180)
        or (uppercase_ratio >= 0.6 and len(text) >= 8)
        or (
            len(text) <= 90
            and line.size >= body_size - 0.1
            and text[:1].isupper()
            and not re.search(r"[\.!?;:]$", text)
            and not re.search(r"://|@", text)
        )
    )


def _drop_first_page_front_matter(lines: list[PdfLine], body_size: float) -> list[PdfLine]:
    if not lines:
        return lines

    # Find first regular paragraph line, then walk back to keep just the nearby title block.
    body_idx = None
    for i, line in enumerate(lines):
        if _is_body_paragraph_line(line, body_size):
            body_idx = i
            break

    if body_idx is not None:
        start_idx = body_idx
        j = body_idx - 1
        while j >= 0:
            line = lines[j]
            if _is_title_block_line(line, body_size):
                start_idx = j
                j -= 1
                continue
            break

        return lines[start_idx:]

    # Fallback for unusual first pages with no paragraph-like line.
    title_start = None
    for i, line in enumerate(lines):
        if _is_title_block_line(line, body_size):
            title_start = i
            break

    if title_start is None:
        return lines
    return lines[title_start:]


def _find_cutoff_after_last_numbered_section(lines: list[PdfLine], body_size: float) -> int:
    numbered: list[tuple[int, int]] = []
    for idx, line in enumerate(lines):
        match = re.match(r"^(\d+)\.\s+\S", line.text)
        if not match:
            continue
        if abs(line.size - body_size) <= 0.4 or line.size >= body_size:
            numbered.append((idx, int(match.group(1))))

    if not numbered:
        return len(lines)

    max_num = max(num for _, num in numbered)
    last_idx = max(idx for idx, num in numbered if num == max_num)

    chars_after_last_heading = 0
    for idx in range(last_idx + 1, len(lines)):
        line = lines[idx]
        chars_after_last_heading += len(line.text)

        if idx - last_idx < 8 or chars_after_last_heading < 900:
            continue

        window = " ".join(lines[j].text for j in range(idx, min(idx + 4, len(lines))))
        if _is_back_matter_anchor(window):
            return idx

        if re.match(r"^\d+\.\s+\S", line.text):
            continue
        if line.size < body_size - 0.1:
            continue

        prev = lines[idx - 1]
        gap = line.top - prev.bottom
        heading_like = line.is_centered or gap >= body_size * 1.6
        if not heading_like:
            continue

        if gap >= body_size * 1.9:
            window = " ".join(lines[j].text for j in range(idx, min(idx + 4, len(lines))))
            if _is_back_matter_anchor(window):
                return idx

    return len(lines)


def _normalize_hyphenation_and_spacing(text: str) -> str:
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(
        r"(?<=[a-ząćęłńóśźż])(?:\s+\d{1,3}){1,3}(?=\s+[a-ząćęłńóśźż])",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?<=[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])\d{1,2}(?=[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])", "", text)
    text = re.sub(r"\s+([,\.;:!?\)])", r"\1", text)
    text = re.sub(r"([\(\[])\s+", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _render_lines_as_text(lines: list[PdfLine]) -> str:
    out: list[str] = []
    last_page = None
    last_bottom = None
    for line in lines:
        if last_page is not None and line.page_no != last_page:
            out.append("\n")
            last_bottom = None

        if last_bottom is not None and (line.top - last_bottom) > line.size * 1.35:
            out.append("\n")

        out.append(line.text + "\n")
        last_page = line.page_no
        last_bottom = line.bottom

    return _normalize_hyphenation_and_spacing("".join(out))


def _compact_numbered_sections(text: str) -> str:
    heading_re = re.compile(r"^\d+\.\s+\S")
    source_lines = [line.strip() for line in text.splitlines()]
    out: list[str] = []
    idx = 0

    while idx < len(source_lines):
        line = source_lines[idx]

        if not line:
            if out and out[-1] != "":
                out.append("")
            idx += 1
            continue

        if heading_re.match(line):
            out.append(line)
            # Keep chapter headings visually separated from paragraph body.
            out.append("")
            idx += 1

            body_parts: list[str] = []
            while idx < len(source_lines):
                current = source_lines[idx]
                if not current:
                    idx += 1
                    continue
                if heading_re.match(current):
                    break
                body_parts.append(current)
                idx += 1

            body = re.sub(r"\s+", " ", " ".join(body_parts)).strip()
            if body:
                out.append(body)

            if out and out[-1] != "":
                out.append("")
            continue

        out.append(line)
        idx += 1

    normalized: list[str] = []
    for line in out:
        if line == "" and (not normalized or normalized[-1] == ""):
            continue
        normalized.append(line)

    while normalized and normalized[-1] == "":
        normalized.pop()

    return "\n".join(normalized)


def _extract_licence_info_from_text(text: str) -> str | None:
    if not text:
        return None

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue

        if "creative commons" in line.lower() or re.search(r"\bcc\s*by", line, flags=re.IGNORECASE):
            match = re.search(
                r"(Creative\s+Commons\s+CC\s*BY(?:-[A-Z]{2})?(?:\s+[0-9]+(?:\.[0-9]+)?)?(?:\s+[^\s,;:\.]{1,30}){0,4})",
                line,
                flags=re.IGNORECASE,
            )
            if match:
                return re.sub(r"\s+", " ", match.group(1)).strip(" .;,")

            match = re.search(
                r"(CC\s*BY(?:-[A-Z]{2})?(?:\s+[0-9]+(?:\.[0-9]+)?)?(?:\s+[^\s,;:\.]{1,30}){0,4})",
                line,
                flags=re.IGNORECASE,
            )
            if match:
                return re.sub(r"\s+", " ", match.group(1)).strip(" .;,")

        if "prawa autorskie" in line.lower():
            return line.strip(" .;,")

    return None


def extract_article_content_from_pdf_bytes(pdf_bytes: bytes) -> tuple[str, str | None]:
    import pdfplumber

    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        pages = [_extract_pdf_page_lines(page, i + 1) for i, page in enumerate(pdf.pages)]

    all_lines = [line for page in pages for line in page]
    if not all_lines:
        return "", None

    full_pdf_text = "\n".join(line.text for line in all_lines if line.text)
    licence_info = _extract_licence_info_from_text(full_pdf_text)

    heading_sizes = [
        line.size
        for line in all_lines
        if re.match(r"^\d+\.\s+\S", line.text)
        and 9.0 <= line.size <= 14.0
    ]
    if heading_sizes:
        body_size = float(median(heading_sizes))
    else:
        bucket = Counter(round(line.size * 2) / 2 for line in all_lines if 8.5 <= line.size <= 13.0)
        body_size = float(max(bucket.items(), key=lambda kv: (kv[1], kv[0]))[0]) if bucket else 11.0

    cleaned_pages: list[list[PdfLine]] = []
    for i, page_lines in enumerate(pages):
        lines = [line for line in page_lines if not _is_running_header_or_footer(line, body_size)]
        if i == 0:
            lines = _drop_first_page_front_matter(lines, body_size)
        lines = [line for line in lines if not _is_bottom_annotation(line, body_size)]
        cleaned_pages.append(lines)

    cleaned_lines = [line for page in cleaned_pages for line in page]
    cutoff = _find_cutoff_after_last_numbered_section(cleaned_lines, body_size)
    cleaned_lines = cleaned_lines[:cutoff]

    while cleaned_lines:
        last = cleaned_lines[-1]
        if re.match(r"^\d+\.\s+\S", last.text):
            break

        looks_heading = (
            last.size >= body_size - 0.2
            and len(last.text) <= 140
            and not re.search(r"[\.!?;:]$", last.text)
            and (last.is_centered or last.text[:1].isupper())
        )
        if not looks_heading:
            break
        cleaned_lines.pop()

    article_text = _compact_numbered_sections(_render_lines_as_text(cleaned_lines))
    return article_text, licence_info


def extract_article_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    article_text, _licence_info = extract_article_content_from_pdf_bytes(pdf_bytes)
    return article_text


class CzasopismaScraper:
    def __init__(
        self,
        delay: float = 0.0,
        timeout: int = 20,
        verbose: bool = False,
        extract_pdf_content: bool = False,
    ) -> None:
        self.delay = max(delay, 0.0)
        self.timeout = timeout
        self.verbose = verbose
        self.extract_pdf_content = extract_pdf_content
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

    def _log(self, message: str, level: int = logging.DEBUG) -> None:
        LOGGER.log(level, message)

    def _get_session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._build_session()
            self._thread_local.session = session
        return session

    def _fetch_soup(self, url: str) -> BeautifulSoup:
        self._log(f"GET {url}")
        response = self._get_session().get(url, timeout=self.timeout)
        response.raise_for_status()
        if self.delay > 0:
            time.sleep(self.delay)
        return BeautifulSoup(response.text, "html.parser")

    def _fetch_pdf_bytes(self, url: str) -> bytes:
        self._log(f"GET {url}")
        response = self._get_session().get(url, timeout=self.timeout)
        response.raise_for_status()
        if self.delay > 0:
            time.sleep(self.delay)
        return response.content

    def _extract_article_content_from_pdf_url(self, pdf_url: str) -> tuple[str | None, str | None]:
        try:
            pdf_bytes = self._fetch_pdf_bytes(pdf_url)
            text, licence_info = extract_article_content_from_pdf_bytes(pdf_bytes)
            return text or None, licence_info
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to extract article text from PDF %s: %s", pdf_url, exc)
            return None, None

    @staticmethod
    def _dedupe_keep_order(items: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out

    @staticmethod
    def _canonical_article_url(url: str) -> str:
        match = ARTICLE_VIEW_RE.search(url)
        if not match:
            return url

        article_id = match.group(1)
        parsed = urlparse(url)
        canonical_path = re.sub(r"/article/view/\d+(?:/\d+)?", f"/article/view/{article_id}", parsed.path)
        canonical = parsed._replace(path=canonical_path, query="", fragment="").geturl()
        return canonical

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

    @staticmethod
    def _is_archive_page_url(url: str) -> bool:
        return bool(ARCHIVE_PAGE_RE.search(urlparse(url).path))

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
        # Prefer explicit issue labels shown on issue pages/cards (e.g. "Tom 26 Nr 1").
        for selector in (
            ".archives__item .desc h2",
            ".archives__item h2",
            ".archives__item--second h2",
            "main h2",
        ):
            tag = issue_soup.select_one(selector)
            if tag:
                text = tag.get_text(" ", strip=True)
                if text:
                    return text

        # Fallback: find a likely issue label anywhere in page text.
        page_text = issue_soup.get_text(" ", strip=True)
        match = re.search(r"\bTom\s+\d+\s+Nr\s+\d+(?:\s*\([^\)]*\))?\b", page_text, flags=re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(0)).strip()

        # Last resort: legacy header/title extraction.
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

        # Most article cards in provided samples use this selector.
        for a_tag in soup.select("a.gray[href*='/article/view/']"):
            href = a_tag.get("href", "").strip()
            if not href:
                continue
            absolute = urljoin(issue_url, href)
            article_links.append(self._canonical_article_url(absolute))

        # Fallback if theme/markup differs.
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

    def _extract_licence_info(self, soup: BeautifulSoup) -> str | None:
        rights = self._meta_content(
            soup,
            "DC.Rights",
            "dc.rights",
            "citation_rights",
        )
        return rights or None

    def _extract_licence_source_urls(self, soup: BeautifulSoup, page_url: str) -> list[str]:
        urls: list[str] = []

        for a_tag in soup.select("a[href]"):
            href = a_tag.get("href", "").strip()
            if not href:
                continue

            absolute = urljoin(page_url, href)
            href_lower = absolute.lower()
            text_lower = a_tag.get_text(" ", strip=True).lower()

            rel_attr = a_tag.get("rel")
            if isinstance(rel_attr, list):
                rel_text = " ".join(str(item) for item in rel_attr).lower()
            else:
                rel_text = str(rel_attr or "").lower()

            if (
                "creativecommons.org" in href_lower
                or "license" in rel_text
                or "licenc" in text_lower
                or "license" in text_lower
            ):
                urls.append(absolute)

        rights = self._extract_licence_info(soup) or ""
        for match in re.finditer(r"https?://\S+", rights):
            urls.append(match.group(0).rstrip(".,;:)]}\"'"))

        inferred_cc_url = self._infer_creative_commons_url(rights)
        if inferred_cc_url:
            urls.append(inferred_cc_url)

        return self._dedupe_keep_order(urls)

    @staticmethod
    def _infer_creative_commons_url(licence_info: str | None) -> str | None:
        if not licence_info:
            return None

        code_match = re.search(r"\bCC\s*BY(?:-[A-Z]{2}){0,2}\b", licence_info, flags=re.IGNORECASE)
        if not code_match:
            return None

        cc_code = code_match.group(0).upper().replace("CC", "").replace(" ", "").strip("-").lower()
        if not cc_code:
            return None

        version_match = re.search(r"\b([1-9](?:\.\d+)?)\b", licence_info)
        version = version_match.group(1) if version_match else "4.0"
        return f"https://creativecommons.org/licenses/{cc_code}/{version}/"

    @staticmethod
    def _article_id_from_url(article_url: str) -> str | None:
        match = ARTICLE_VIEW_RE.search(article_url)
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _safe_path_part(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
        cleaned = cleaned.strip("._-")
        return cleaned or "item"

    def export_article_bundle(self, record: ArticleRecord, export_root: Path, index: int) -> None:
        article_id = self._article_id_from_url(record.article_url) or f"idx_{index + 1:04d}"
        folder_name = f"{index + 1:04d}_{self._safe_path_part(article_id)}"
        article_dir = export_root / folder_name
        article_dir.mkdir(parents=True, exist_ok=True)

        issue_name = record.journal_name_with_issue or record.journal_name or ""
        (article_dir / "issue_full_name.txt").write_text(issue_name.strip() + "\n", encoding="utf-8")

        pdf_bytes: bytes | None = None
        article_text = record.article_text
        licence_info = record.licence_info

        if record.pdf_download_url:
            try:
                pdf_bytes = self._fetch_pdf_bytes(record.pdf_download_url)
                (article_dir / "article.pdf").write_bytes(pdf_bytes)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Failed to download PDF for %s: %s", record.article_url, exc)

        if pdf_bytes and not article_text:
            try:
                extracted_text, pdf_licence_info = extract_article_content_from_pdf_bytes(pdf_bytes)
                article_text = extracted_text or None
                if pdf_licence_info:
                    licence_info = pdf_licence_info
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Failed to extract article text during bundle export for %s: %s", record.article_url, exc)

        (article_dir / "extracted_text.txt").write_text((article_text or "").strip() + "\n", encoding="utf-8")

        licence_source_urls: list[str] = []
        try:
            soup = self._fetch_soup(record.article_url)
            licence_source_urls = self._extract_licence_source_urls(soup, record.article_url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to read licence source URLs for %s: %s", record.article_url, exc)

        inferred_cc_url = self._infer_creative_commons_url(licence_info)
        if inferred_cc_url:
            licence_source_urls.append(inferred_cc_url)
        licence_source_urls = self._dedupe_keep_order(licence_source_urls)

        licence_lines = [
            f"Article URL: {record.article_url}",
            f"PDF URL: {record.pdf_download_url or ''}",
            f"Licence: {licence_info or ''}",
            "Licence source URLs:",
        ]
        if licence_source_urls:
            for url in licence_source_urls:
                licence_lines.append(f"- {url}")
        else:
            licence_lines.append("- (none found)")
        (article_dir / "licence_and_sources.txt").write_text("\n".join(licence_lines) + "\n", encoding="utf-8")

        metadata = asdict(record)
        metadata.pop("article_text", None)
        metadata["licence_info"] = licence_info
        metadata_text = json.dumps(metadata, ensure_ascii=False, indent=2)
        (article_dir / "metadata.json").write_text(metadata_text + "\n", encoding="utf-8")
    @staticmethod
    def _normalize_language(value: str | None) -> str | None:
        if not value:
            return None

        token = value.strip().lower().replace("-", "_")
        token = token.split(";")[0].split(",")[0].strip()

        lang_map = {
            "pl": "Polish",
            "pl_pl": "Polish",
            "pol": "Polish",
            "en": "English",
            "en_us": "English",
            "eng": "English",
        }
        if token in lang_map:
            return lang_map[token]

        # Keep human-readable values if the source already provides one.
        if token in {"polish", "english"}:
            return token.capitalize()

        return None

    def _extract_article_language(self, soup: BeautifulSoup) -> str:
        raw = self._meta_content(
            soup,
            "citation_language",
            "DC.Language",
            "dc.language",
        )
        normalized = self._normalize_language(raw)

        title = self._meta_content(soup, "citation_title")
        if not title:
            heading = soup.select_one("h1.page_title, h1")
            if heading:
                title = heading.get_text(" ", strip=True)

        inferred = self._infer_language_from_title(title)
        if inferred and (normalized is None or normalized == "Polish"):
            return inferred

        return normalized or "Polish"

    @staticmethod
    def _infer_language_from_title(title: str | None) -> str | None:
        if not title:
            return None

        lowered = title.lower()
        if re.search(r"[ąćęłńóśźż]", lowered):
            return None

        words = re.findall(r"[a-zA-Z']+", lowered)
        if len(words) < 4:
            return None

        english_markers = {
            "the",
            "and",
            "of",
            "in",
            "on",
            "to",
            "for",
            "with",
            "from",
            "civil",
            "proceedings",
            "service",
            "selected",
            "issues",
            "status",
            "law",
            "code",
            "concept",
            "summary",
            "polish",
        }
        marker_hits = sum(1 for word in words if word in english_markers)
        if marker_hits >= 2 and marker_hits / len(words) >= 0.2:
            return "English"

        return None

    def _extract_journal_name(self, soup: BeautifulSoup) -> str | None:
        journal = self._meta_content(
            soup,
            "citation_journal_title",
            "DC.Source",
            "dc.source",
            "og:description",
        )
        if journal:
            return journal

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

        download_link = soup.select_one("a.download[href*='/article/download/']")
        if download_link and download_link.get("href"):
            return urljoin(article_url, download_link.get("href", "").strip())

        btn_link = soup.select_one("a.btnDownload[href]")
        if btn_link and btn_link.get("href"):
            href = urljoin(article_url, btn_link.get("href", "").strip())
            if "/article/download/" in href:
                return href
            if "/article/view/" in href:
                converted = self._view_to_download_url(href)
                if converted:
                    return converted

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
        soup = self._fetch_soup(article_url)

        journal_name = self._extract_journal_name(soup)
        journal_name_with_issue = self._compose_journal_name_with_issue(journal_name, issue_display_name)
        article_language = self._extract_article_language(soup)
        licence_info = self._extract_licence_info(soup)
        pdf_link = self._extract_pdf_link(soup, article_url)
        article_text = None
        if self.extract_pdf_content and pdf_link:
            article_text, pdf_licence_info = self._extract_article_content_from_pdf_url(pdf_link)
            if pdf_licence_info:
                licence_info = pdf_licence_info

        return ArticleRecord(
            article_url=article_url,
            journal_name=journal_name,
            journal_name_with_issue=journal_name_with_issue,
            article_language=article_language,
            licence_info=licence_info,
            pdf_download_url=pdf_link,
            article_text=article_text,
        )


def export_article_bundles(scraper: CzasopismaScraper, records: list[ArticleRecord], export_dir: str) -> None:
    root = Path(export_dir)
    root.mkdir(parents=True, exist_ok=True)

    for index, record in enumerate(records):
        scraper.export_article_bundle(record, root, index)

    LOGGER.info("Exported %d article bundle directories to %s", len(records), root)


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

    # CSV
    fieldnames = [
        "article_url",
        "journal_name",
        "journal_name_with_issue",
        "article_language",
        "licence_info",
        "pdf_download_url",
        "article_text",
    ]
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
        description=(
            "Scrape Czasopisma/OJS pages and return article URL, journal name, PDF link, and optional extracted article text."
        )
    )
    parser.add_argument(
        "--archive-url",
        default="https://czasopisma.uksw.edu.pl/index.php/zp/issue/archive",
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
        help="Show progress logs on stderr.",
    )
    parser.add_argument(
        "--extract-pdf-content",
        action="store_true",
        help="Extract article text from PDF links and include it as article_text.",
    )
    parser.add_argument(
        "--skip-english-articles",
        action="store_true",
        help="Skip records detected as English-language articles.",
    )
    parser.add_argument(
        "--article-export-dir",
        default=None,
        help=(
            "Create per-article folders in this directory with article.pdf, issue_full_name.txt, "
            "licence_and_sources.txt, extracted_text.txt, and metadata.json (without extracted text)."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    LOGGER.info("Starting scraper")

    scraper = CzasopismaScraper(
        delay=args.delay,
        timeout=args.timeout,
        verbose=args.verbose,
        extract_pdf_content=args.extract_pdf_content,
    )

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
                print(f"Warning: failed to scrape article {entry.article_url}: {exc}", file=sys.stderr)

    records = [record for record in records_by_index if record is not None]

    if args.skip_english_articles:
        before_count = len(records)
        records = [record for record in records if record.article_language != "English"]
        skipped = before_count - len(records)
        LOGGER.info("Skipped %d English-language records", skipped)

    LOGGER.info("Finished scraping. Successful records: %d", len(records))
    write_output(records, args.output, args.format, args.pretty)

    if args.article_export_dir:
        export_article_bundles(scraper, records, args.article_export_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
