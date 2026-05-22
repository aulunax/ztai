import json
import logging
from pathlib import Path
import re
import time

import pdfplumber
from polyglot.detect import Detector

from utils.issue import ArticleData

from .extractor import Extractor


logging.getLogger("polyglot.detect.base").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Journals PDF tuning constants (keep separate from other extractors).
JOURNALS_SIZE_TOLERANCE = 0.8 # 0.05
JOURNALS_SEPARATOR_LINEWIDTH = 0.5
JOURNALS_NEWLINE_LINEWIDTH = 2.0
JOURNALS_ANNOTATION_MAX_SIZE = 7.0
JOURNALS_ANNOTATION_BASELINE_DELTA = 3.0
JOURNALS_STOP_MARKERS = ["bibliografia", "summary"]
JOURNALS_LINE_Y_TOLERANCE = 2.0
JOURNALS_TABLE_START_RE = re.compile(
    r"^(tabela|rysunek|wykres|schemat|mapa)\s+\d+\s?$",
    re.IGNORECASE,
)
JOURNALS_SOURCE_RE = re.compile(r"^źródło:", re.IGNORECASE)
JOURNALS_CITATION_STOP_RE = re.compile(r",\s*[A-ZĄĆĘŁŃÓŚŹŻ]\.\s+.*\(\d{4}\)")
JOURNALS_CITATION_STOP_MARKER = "__STOP_CITATION__"
JOURNALS_LANGDETECT_LOG_NAME = "langdetect_removed.txt"
JOURNALS_MIN_LANGDETECT_CHARS = 500
JOURNALS_BAD_FRAGMENT_LOG_NAME = "bad_fragments.log"

def is_stop_marker(text: str) -> bool:
    return text.strip().lower() in JOURNALS_STOP_MARKERS

ARTICLE_VIEW_RE = re.compile(r"/article/view/(\d+)")

class JournalsExtractor(Extractor):
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
        self.notified_about_matrix = False

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

    # docker run     -it     --rm     --gpus all   -p 8119:8119     -v $(pwd)/vllm_config.yml:/tmp/vllm_config.yml:ro  ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddleocr-genai-vllm-server:latest-nvidia-gpu-offline     paddleocr genai_server --model_name PaddleOCR-VL-1.5-0.9B --host 0.0.0.0 --port 8119 --backend vllm --backend_config /tmp/vllm_config.yml
    def _extract_text_from_pdf_ocr(self, pdf_path: Path) -> str:
        from paddleocr import PaddleOCRVL
        try:
            pipeline = PaddleOCRVL(vl_rec_backend="vllm-server", vl_rec_server_url="http://localhost:8119/v1")

            output = pipeline.predict(input=str(pdf_path), lang="pl", text_det_thresh=0.3, text_det_box_thresh=0.6, text_det_unclip_ratio=2.0, text_rec_score_thresh=0.0)

            pages_res = list(output)

            output = pipeline.restructure_pages(pages_res, merge_tables=True, relevel_titles=True, concatenate_pages=True)

            pdf_full = output[0].json['res']

            blocks = self._extract_blocks(pdf_full)

            filtered_blocks = []
            for block in blocks:
                if block["label"] in ("text", "paragraph_title"):
                    block["text"] = self._strip_annotations(block["text"])
                    filtered_blocks.append(block)

            cut_index = None

            for i, block in enumerate(filtered_blocks):
                if is_stop_marker(block["text"]):
                    cut_index = i
                    break

            if cut_index is not None:
                filtered_blocks = filtered_blocks[:cut_index]

            # merge consecutive text blocks
            merged_text_blocks = []
            current_text_blocks = []
            for block in filtered_blocks:
                if block["label"] == "paragraph_title":
                    if current_text_blocks:
                        merged_text_blocks.append(" ".join(block["text"] for block in current_text_blocks))
                        current_text_blocks = []
                    merged_text_blocks.append(block["text"])
                else:
                    current_text_blocks.append(block)

            if current_text_blocks:
                merged_text_blocks.append(" ".join(block["text"] for block in current_text_blocks))


            # create a text file with just the text content of the blocks, separated by newlines
            text_output = "\n\n".join(text for text in merged_text_blocks)

            return text_output

            with Path("test_paddle_output_my.json").open("w", encoding="utf-8") as f:
                json.dump(pdf_full, f, ensure_ascii=False, indent=2)

            for res in output:
                print("a")
                res.save_to_json(save_path="test_paddle_output.json")  # get the structured result as a dict
                res.save_to_markdown(save_path="test_paddle_output.md")

            # save output to file
            with Path("test_paddle_output_my.json").open("w", encoding="utf-8") as f:
                json.dump(pdf_full, f, ensure_ascii=False, indent=2)

        except Exception as exc:
            self.logger.warning("PaddleOCR extraction failed for %s: %s", pdf_path, exc)
            return ""

    def extract(self, data, limit: int = None, start_at_index: int = 0):
        self.logger.info("Phase 1: downloading PDFs")
        articles = data[:limit+start_at_index] if limit else data
        export_root = self.output_dir / "data" / "journals"
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

                article_dir = self._build_article_dir(article, index, export_root)
                pdf_path = article_dir / "article.pdf"
                article_dir.mkdir(parents=True, exist_ok=True)

                if not article.pdf_url:
                    self.logger.warning("Missing PDF URL for %s", article.url or f"index {index}")
                    continue

                phase_start = time.perf_counter()
                self._download_pdf(article.pdf_url, pdf_path)
                download_times.append(time.perf_counter() - phase_start)

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
            except Exception as exc:
                self.logger.warning("Failed to extract PDF text for %s: %s", article_dir, exc)
                continue
            extraction_times.append(time.perf_counter() - phase_start)

            (article_dir / "extracted_text.txt").write_text(extracted_text.strip() + "\n", encoding="utf-8")

            metadata_path = article_dir / "metadata.json"
            metadata_text = json.dumps(article.to_json(), ensure_ascii=False, indent=2)
            metadata_path.write_text(metadata_text + "\n", encoding="utf-8")