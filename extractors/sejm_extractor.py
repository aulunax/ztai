import json
from pathlib import Path
import re
import time

from utils.issue import ArticleData

from .extractor import Extractor

DOCUMENT_ID_RE = re.compile(r"(?:documentId=|OpenAgent&)([A-Fa-f0-9]+)")

class SejmExtractor(Extractor):
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

    @staticmethod
    def _document_id_from_url(url: str | None) -> str | None:
        if not url:
            return None
        match = DOCUMENT_ID_RE.search(url)
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _safe_path_part(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
        cleaned = cleaned.strip("._-")
        return cleaned or "item"

    def _build_article_dir(self, article: ArticleData, index: int, root: Path) -> Path:
        article_id = (
            self._document_id_from_url(article.url)
            or self._document_id_from_url(article.pdf_url)
            or f"idx_{index + 1:04d}"
        )
        folder_name = f"{index + 1:04d}_{self._safe_path_part(article_id)}"
        return root / folder_name

    def extract(self, data, limit: int = None, start_at_index: int = 0):
        self.logger.info("Phase 1: downloading PDFs")
        articles = data[:limit + start_at_index] if limit else data
        export_root = self.output_dir / "data" / "sejm"
        export_root.mkdir(parents=True, exist_ok=True)

        total_articles = len(articles)

        if self.skip_download:
            self.logger.info("Phase: downloading PDFs (skipped)")
        else:
            download_times: list[float] = []
            for index, article in enumerate(articles):
                if download_times:
                    avg = sum(download_times) / len(download_times)
                    remaining = total_articles - (index + 1)
                    eta = self._format_eta(avg * remaining)
                    self.logger.info("Downloading PDF %d/%d (ETA: %s)", index + 1, total_articles, eta)
                else:
                    self.logger.info("Downloading PDF %d/%d", index + 1, total_articles)

                if not isinstance(article, ArticleData):
                    self.logger.warning("Skipping non-ArticleData entry at index %d", index)
                    continue

                if not article.pdf_url:
                    self.logger.warning("Missing PDF URL for %s", article.url or f"index {index}")
                    continue

                if not article.url or article.url == article.pdf_url:
                    self.logger.info("Skipping download for %s (article URL matches PDF URL)", article.pdf_url)
                    continue

                article_dir = self._build_article_dir(article, index, export_root)
                pdf_path = article_dir / "article.pdf"
                article_dir.mkdir(parents=True, exist_ok=True)

                phase_start = time.perf_counter()
                self._download_pdf(article.pdf_url, pdf_path)
                download_times.append(time.perf_counter() - phase_start)

        if self.skip_text_extraction:
            self.logger.info("Phase 2: extracting text (skipped)")
            return

        self.logger.info("Phase 2: extracting text")
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
            except Exception as exc:
                self.logger.warning("Failed to extract PDF text for %s: %s", article_dir, exc)
                continue
            extraction_times.append(time.perf_counter() - phase_start)

            (article_dir / "extracted_text.txt").write_text(extracted_text.strip() + "\n", encoding="utf-8")

            metadata_path = article_dir / "metadata.json"
            metadata_text = json.dumps(article.to_json(), ensure_ascii=False, indent=2)
            metadata_path.write_text(metadata_text + "\n", encoding="utf-8")