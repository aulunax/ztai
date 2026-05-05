from pathlib import Path
import re
import time

from utils.issue import ArticleData

from .extractor import Extractor


ARTICLE_VIEW_RE = re.compile(r"/article/view/(\d+)")

class JournalsExtractor(Extractor):
    def __init__(self, output_dir: str | None = None, skip_download: bool = False):
        super().__init__()
        self.output_dir = Path(output_dir) if output_dir else Path(".")
        self.skip_download = skip_download

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

    def extract(self, data, limit: int = None):
        if self.skip_download:
            self.logger.info("Phase: downloading PDFs (skipped)")
            return

        self.logger.info("Phase: downloading PDFs")
        articles = data[:limit] if limit else data
        export_root = self.output_dir / "data" / "journals"
        export_root.mkdir(parents=True, exist_ok=True)

        total_articles = len(articles)
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

            article_dir = self._build_article_dir(article, index, export_root)
            pdf_path = article_dir / "article.pdf"
            article_dir.mkdir(parents=True, exist_ok=True)

            if not article.pdf_url:
                self.logger.warning("Missing PDF URL for %s", article.url or f"index {index}")
                continue

            phase_start = time.perf_counter()
            self._download_pdf(article.pdf_url, pdf_path)
            download_times.append(time.perf_counter() - phase_start)