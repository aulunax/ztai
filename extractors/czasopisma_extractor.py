from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
from statistics import median
import time

from bs4 import BeautifulSoup
import pdfplumber
import requests

from utils.issue import ArticleData

from .extractor import Extractor


ARTICLE_VIEW_RE = re.compile(r"/article/view/(\d+)")


class CzasopismaExtractor(Extractor):
    def __init__(
        self,
        output_dir: str | None = None,
        skip_download: bool = False,
        skip_text_extraction: bool = False,
    ):
        super().__init__()
        self.output_dir = Path(output_dir) if output_dir else Path(".")
        self.skip_download = skip_download
        self.skip_text_extraction = skip_text_extraction

    def _fetch_soup(self, url: str) -> BeautifulSoup | None:
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            self.logger.warning("Failed to fetch page %s: %s", url, exc)
            return None


    @staticmethod
    def _meta_content(soup: BeautifulSoup, *names: str) -> str | None:
        for name in names:
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                value = tag["content"].strip()
                if value:
                    return value
        return None

    def _extract_page_licence_info(self, article_url: str) -> str | None:
        soup = self._fetch_soup(article_url)
        if not soup:
            return None
        rights = self._meta_content(
            soup,
            "DC.Rights",
            "dc.rights",
            "citation_rights",
        )
        return rights or None

    @staticmethod
    def _article_id_from_url(article_url: str | None) -> str | None:
        if not article_url:
            return None
        match = ARTICLE_VIEW_RE.search(article_url)
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _safe_path_part(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
        cleaned = cleaned.strip("._-")
        return cleaned or "item"

    def _build_article_dir(self, article: ArticleData, index: int, root: Path) -> Path:
        article_id = self._article_id_from_url(article.url) or f"idx_{index + 1:04d}"
        folder_name = f"{index + 1:04d}_{self._safe_path_part(article_id)}"
        return root / folder_name

    def extract(self, data, limit: int = None, start_at_index: int = 0):
        self.logger.info("Extracting data from PDF content...")
        articles = data[:limit] if limit else data
        export_root = self.output_dir / "data" / "czasopisma"
        export_root.mkdir(parents=True, exist_ok=True)

        total_articles = len(articles)
        if not self.skip_download:
            self.logger.info("Phase 1: downloading PDFs")
            for index, article in enumerate(articles):
                self.logger.info("Downloading PDF %d/%d", index + 1, total_articles)
                if not isinstance(article, ArticleData):
                    self.logger.warning("Skipping non-ArticleData entry at index %d", index)
                    continue

                article_dir = self._build_article_dir(article, index, export_root)
                pdf_path = article_dir / "article.pdf"
                article_dir.mkdir(parents=True, exist_ok=True)
                if article.pdf_url:
                    self._download_pdf(article.pdf_url, pdf_path)
                else:
                    self.logger.warning("Missing PDF URL for %s", article.url or f"index {index}")

        if self.skip_text_extraction:
            self.logger.info("Phase 2: extracting text and license (skipped)")
            return

        self.logger.info("Phase 2: extracting text and license")
        extraction_times: list[float] = []
        for index, article in enumerate(articles):
            if index < start_at_index:
                continue
            if extraction_times:
                avg_text = sum(extraction_times) / len(extraction_times)
                remaining = total_articles - (index + 1)
                eta = self._format_eta(avg_text * remaining)
                self.logger.info("Extracting article %d/%d (ETA: %s)", index + 1, total_articles, eta)
            else:
                self.logger.info("Extracting article %d/%d", index + 1, total_articles)
            if not isinstance(article, ArticleData):
                self.logger.warning("Skipping non-ArticleData entry at index %d", index)
                continue

            article_dir = self._build_article_dir(article, index, export_root)
            pdf_path = article_dir / "article.pdf"

            if not pdf_path.exists():
                self.logger.warning("PDF not found for %s", article_dir)
                continue

            phase_start = time.perf_counter()
            try:
                extracted_text = super()._extract_text_from_pdf_ocr(pdf_path)
                pdf_licence = None
                # extracted_text, pdf_licence = _extract_article_content_from_pdf_path(pdf_path)
            except Exception as exc:
                self.logger.warning("Failed to extract PDF text for %s: %s", article_dir, exc)
                continue
            extraction_times.append(time.perf_counter() - phase_start)

            if pdf_licence:
                article.license = pdf_licence
            elif not article.license and article.url:
                page_licence = self._extract_page_licence_info(article.url)
                if page_licence:
                    article.license = page_licence

            (article_dir / "extracted_text.txt").write_text(extracted_text.strip() + "\n", encoding="utf-8")

            metadata_path = article_dir / "metadata.json"
            metadata_text = json.dumps(article.to_json(), ensure_ascii=False, indent=2)
            metadata_path.write_text(metadata_text + "\n", encoding="utf-8")