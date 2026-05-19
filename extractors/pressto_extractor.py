import json
from pathlib import Path
import re
import time

import pdfplumber
from langdetect import DetectorFactory, LangDetectException, detect, detect_langs

from utils.issue import ArticleData

from .extractor import Extractor

# Tuning constants for size grouping and annotation filtering.
SIZE_TOLERANCE = 0.05
SEPARATOR_LINEWIDTH = 0.5
ANNOTATION_MAX_SIZE = 6.0
ANNOTATION_BASELINE_DELTA = 4.0
STOP_MARKERS = ["references / bibliografia", "summary"]
LINE_Y_TOLERANCE = 2.0
TABLE_START_RE = re.compile(r"^(tabela|rysunek)\s+\d+", re.IGNORECASE)
SOURCE_RE = re.compile(r"^źródło:", re.IGNORECASE)
CITATION_STOP_RE = re.compile(r",\s*[A-ZĄĆĘŁŃÓŚŹŻ]\.\s+.*\(\d{4}\)")
CITATION_STOP_MARKER = "__STOP_CITATION__"
LANGDETECT_LOG_NAME = "langdetect_removed.txt"
MIN_LANGDETECT_CHARS = 500

DetectorFactory.seed = 0



ARTICLE_VIEW_RE = re.compile(r"/article/view/(\d+)")

class PresstoExtractor(Extractor):
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

    # Helper to normalize line positions relative to the top of the page.
    def _line_pos_from_top(self, line: dict, page_height: float) -> float | None:
        if "top" in line and line["top"] is not None:
            return float(line["top"])
        y0 = line.get("y0")
        y1 = line.get("y1")
        if y0 is None or y1 is None:
            return None
        return page_height - max(float(y0), float(y1))


    # Helper to get character bounds relative to the top of the page.
    def _char_bounds_from_top(self, char: dict, page_height: float) -> tuple[float | None, float | None]:
        if "top" in char and "bottom" in char:
            return float(char["top"]), float(char["bottom"])
        y0 = char.get("y0")
        y1 = char.get("y1")
        if y0 is None or y1 is None:
            return None, None
        top = page_height - max(float(y0), float(y1))
        bottom = page_height - min(float(y0), float(y1))
        return top, bottom


    # Detect separator lines and compute ignore cutoffs.
    def _compute_cutoffs(self, page: pdfplumber.page.Page) -> tuple[float | None, float | None]:
        page_height = float(page.height)
        bottom_cutoff = None
        top_cutoff = None
        for line in page.lines:
            linewidth = line.get("linewidth")
            if linewidth is None or abs(float(linewidth) - SEPARATOR_LINEWIDTH) > 1e-6:
                continue
            line_top = self._line_pos_from_top(line, page_height)
            if line_top is None:
                continue
            if line_top > page_height * 0.5:
                bottom_cutoff = line_top if bottom_cutoff is None else min(bottom_cutoff, line_top)
            if line_top < page_height * 0.25:
                top_cutoff = line_top if top_cutoff is None else max(top_cutoff, line_top)
        return bottom_cutoff, top_cutoff


    # Filter out characters outside cutoffs or likely annotations.
    def _filter_chars(self, page: pdfplumber.page.Page) -> list[dict]:
        page_height = float(page.height)
        bottom_cutoff, top_cutoff = self._compute_cutoffs(page)
        filtered_chars: list[dict] = []
        prev_base_y0 = None
        for char in page.chars:
            top, bottom = self._char_bounds_from_top(char, page_height)
            if top is None or bottom is None:
                continue
            if bottom_cutoff is not None and top > bottom_cutoff:
                continue
            if top_cutoff is not None and bottom < top_cutoff:
                continue
            size = char.get("size")
            base_y0 = char.get("y0")
            if (
                size is not None
                and base_y0 is not None
                and size < ANNOTATION_MAX_SIZE
                and prev_base_y0 is not None
                and (float(base_y0) - float(prev_base_y0)) >= ANNOTATION_BASELINE_DELTA
            ):
                continue
            filtered_chars.append(char)
            if base_y0 is not None:
                prev_base_y0 = float(base_y0)
        return filtered_chars


    # Remove table/figure blocks between heading and source line.
    def _filter_table_blocks(self, page: pdfplumber.page.Page, chars: list[dict]) -> list[dict]:
        page_height = float(page.height)
        ordered: list[tuple[float, float, dict]] = []
        for char in chars:
            top, _ = self._char_bounds_from_top(char, page_height)
            x0 = char.get("x0")
            if top is None or x0 is None:
                continue
            ordered.append((float(top), float(x0), char))

        ordered.sort(key=lambda item: (item[0], item[1]))

        lines: list[tuple[float, list[dict]]] = []
        current_top = None
        current_chars: list[dict] = []
        for top, _x0, char in ordered:
            if current_top is None or abs(top - current_top) > LINE_Y_TOLERANCE:
                if current_chars:
                    lines.append((current_top, current_chars))
                current_top = top
                current_chars = [char]
            else:
                current_chars.append(char)
        if current_chars:
            lines.append((current_top, current_chars))

        kept: list[dict] = []
        in_block = False
        for _top, line_chars in lines:
            line_chars_sorted = sorted(line_chars, key=lambda c: float(c.get("x0", 0.0)))
            line_text = "".join([c.get("text", "") for c in line_chars_sorted])
            normalized = " ".join(line_text.split())
            if not normalized:
                kept.extend(line_chars_sorted)
                continue

            if not in_block and TABLE_START_RE.match(normalized):
                in_block = True
                continue
            if in_block:
                if SOURCE_RE.match(normalized):
                    in_block = False
                continue

            kept.extend(line_chars_sorted)

        return kept


    # Merge consecutive characters by font size into text blocks.
    def _merge_by_size(
        self,
        chars: list[dict],
        page_index: int,
        page_width: float,
        state: tuple[float | None, bool | None, list[str], int | None] | None = None,
    ) -> tuple[list[tuple[str, int]], tuple[float | None, bool | None, list[str], int | None]]:
        def is_bold(fontname: str | None) -> bool:
            if not fontname:
                return False
            return "bold" in fontname.lower()

        def is_letter(value: str) -> bool:
            return bool(value) and value.isalpha()

        merged_lines: list[tuple[str, int]] = []
        if state:
            current_size, current_bold, current_text, last_page_index = state
        else:
            current_size = None
            current_bold = None
            current_text = []
            last_page_index = page_index
        prev_char = None
        for char_index, char in enumerate(chars):
            size = char.get("size")
            text = char.get("text", "")
            bold = is_bold(char.get("fontname"))
            bold_for_break = bold if text.strip() else current_bold
            if current_size is None:
                current_size = size
                current_bold = bold
            last_page_index = page_index

            size_changed = (
                size is None
                or current_size is None
                or abs(size - current_size) > SIZE_TOLERANCE
            )
            bold_changed = bold_for_break != current_bold
            if size_changed or bold_changed:
                if size_changed:
                    print(
                        "Page {page} char {idx}: size {old:.3f} -> {new:.3f}"
                        .format(
                            page=page_index,
                            idx=char_index,
                            old=current_size if current_size is not None else -1.0,
                            new=size if size is not None else -1.0,
                        )
                    )
                if bold_changed:
                    print(
                        "Page {page} char {idx}: bold {old} -> {new}"
                        .format(page=page_index, idx=char_index, old=current_bold, new=bold_for_break)
                    )
                if current_text:
                    line_text = "".join(current_text).strip()
                    if line_text:
                        if CITATION_STOP_RE.search(line_text):
                            merged_lines.append((CITATION_STOP_MARKER, last_page_index or page_index))
                        else:
                            merged_lines.append((line_text, last_page_index or page_index))
                    current_text = []
                merged_lines.append(("", last_page_index or page_index))
                current_text.append(text)
                current_size = size
                current_bold = bold_for_break
                prev_char = None
                continue

            if prev_char and prev_char.get("text") == "-" and current_text and current_text[-1] == "-":
                prev_x0 = prev_char.get("x0")
                curr_x0 = char.get("x0")
                prev_y0 = prev_char.get("y0")
                curr_y0 = char.get("y0")
                base_size = float(size or current_size or 0.0)
                if (
                    prev_x0 is not None
                    and curr_x0 is not None
                    and prev_y0 is not None
                    and curr_y0 is not None
                    and (float(prev_x0) - float(curr_x0)) >= page_width * 0.2
                    and (float(prev_y0) - float(curr_y0)) >= base_size * 0.6
                    and is_letter(text)
                ):
                    current_text.pop()

            if (
                prev_char
                and prev_char.get("text") == "-"
                and current_text
                and current_text[-1] == "-"
            ):
                prev_x1 = prev_char.get("x1")
                curr_x0 = char.get("x0")
                if (
                    prev_x1 is not None
                    and curr_x0 is not None
                    and float(prev_x1) >= page_width * 0.9
                    and float(curr_x0) <= page_width * 0.5
                    and is_letter(text)
                ):
                    current_text.pop()

            current_text.append(text)
            prev_char = char

        if current_text:
            line_text = "".join(current_text).strip()
            if line_text and CITATION_STOP_RE.search(line_text):
                merged_lines.append((CITATION_STOP_MARKER, last_page_index or page_index))
                current_text = []
        return merged_lines, (current_size, current_bold, current_text, last_page_index)


    # Truncate output when a stop marker is detected.
    def _truncate_on_markers(self, lines: list[tuple[str, int]]) -> tuple[list[tuple[str, int]], bool]:
        kept: list[tuple[str, int]] = []
        for line, page_no in lines:
            if line == CITATION_STOP_MARKER:
                return kept, True
            normalized = " ".join(line.lower().split())
            normalized_slash = normalized.replace(" / ", "/")
            if any(marker in normalized for marker in STOP_MARKERS) or any(
                marker.replace(" / ", "/") in normalized_slash for marker in STOP_MARKERS
            ):
                return kept, True
            kept.append((line, page_no))
        return kept, False

    def _filter_english_lines(
        self,
        lines: list[tuple[str, int]],
        pdf_path: Path,
        log_path: Path,
    ) -> list[str]:
        kept: list[str] = []
        removed: list[tuple[int, str, str]] = []

        for line, page_no in lines:
            if not line.strip():
                kept.append(line)
                continue
            if len(line) < MIN_LANGDETECT_CHARS:
                kept.append(line)
                continue
            try:
                lang = detect(line)
                langs = detect_langs(line)
            except LangDetectException:
                kept.append(line)
                continue
            if lang == "en":
                probs = ",".join(f"{item.lang}:{item.prob:.3f}" for item in langs)
                removed.append((page_no, line, probs))
                continue
            kept.append(line)

        if removed:
            with open(log_path, "a", encoding="utf-8") as f:
                for page_no, line, probs in removed:
                    f.write(f"{pdf_path.name}\tpage {page_no}\t{probs}\t{line}\n")

        return kept
    
    def _extract_text_from_pdf(self, pdf_path: Path) -> str:
        with pdfplumber.open(pdf_path) as pdf:
            all_pages_output: list[tuple[str, int]] = []
            stop_all = False
            merge_state: tuple[float | None, bool | None, list[str], int | None] | None = None
            for page_index, page in enumerate(pdf.pages, start=1):
                if stop_all:
                    break
                if page_index == 12:
                    with open("page_12_chars.json", "w", encoding="utf-8") as f:
                        json.dump(page.chars, f, ensure_ascii=False, indent=2)
                filtered_chars = self._filter_chars(page)
                filtered_chars = self._filter_table_blocks(page, filtered_chars)
                merged_lines, merge_state = self._merge_by_size(
                    filtered_chars,
                    page_index,
                    float(page.width),
                    merge_state,
                )
                trimmed_lines, stop_all = self._truncate_on_markers(merged_lines)
                all_pages_output.extend(trimmed_lines)

            if merge_state and merge_state[2]:
                tail_text = "".join(merge_state[2]).strip()
                if tail_text:
                    all_pages_output.append((tail_text, merge_state[3] or 1))

            # Write the merged output to a text file.
            log_path = self.output_dir / LANGDETECT_LOG_NAME
            filtered_output = self._filter_english_lines(all_pages_output, pdf_path, log_path)
            cleaned_output = [line.rstrip() for line in filtered_output]
            return "\n".join(cleaned_output)

    def extract(self, data, limit: int = None):
        self.logger.info("Phase 1: downloading PDFs")
        articles = data[:limit] if limit else data
        export_root = self.output_dir / "data" / "pressto"
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

        self.logger.info("Phase 2: extracting text and license")
        extraction_times: list[float] = []
        for index, article in enumerate(articles):
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
                extracted_text = self._extract_text_from_pdf(pdf_path)
            except Exception as exc:
                self.logger.warning("Failed to extract PDF text for %s: %s", article_dir, exc)
                continue
            extraction_times.append(time.perf_counter() - phase_start)

            (article_dir / "extracted_text.txt").write_text(extracted_text.strip() + "\n", encoding="utf-8")

            metadata_path = article_dir / "metadata.json"
            metadata_text = json.dumps(article.to_json(), ensure_ascii=False, indent=2)
            metadata_path.write_text(metadata_text + "\n", encoding="utf-8")