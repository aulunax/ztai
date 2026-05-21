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

# Tuning constants for size grouping and annotation filtering.
SIZE_TOLERANCE = 0.8 # 0.05
SEPARATOR_LINEWIDTH = 0.5
NEWLINE_LINEWIDTH = 2.0
ANNOTATION_MAX_SIZE = 7.0
ANNOTATION_BASELINE_DELTA = 3.0
STOP_MARKERS = ["references / bibliografia", "summary"]
LINE_Y_TOLERANCE = 2.0
TABLE_START_RE = re.compile(
    r"^(tabela|rysunek|wykres|schemat|mapa)\s+\d+\s?$",
    re.IGNORECASE,
)
SOURCE_RE = re.compile(r"^źródło:", re.IGNORECASE)
CITATION_STOP_RE = re.compile(r",\s*[A-ZĄĆĘŁŃÓŚŹŻ]\.\s+.*\(\d{4}\)")
CITATION_STOP_MARKER = "__STOP_CITATION__"
LANGDETECT_LOG_NAME = "langdetect_removed.txt"
MIN_LANGDETECT_CHARS = 500
BAD_FRAGMENT_LOG_NAME = "bad_fragments.log"


ARTICLE_VIEW_RE = re.compile(r"/article/view/(\d+)")

class PresstoExtractor(Extractor):
    def __init__(self, output_dir: str | None = None, skip_download: bool = False):
        super().__init__()
        self.output_dir = Path(output_dir) if output_dir else Path(".")
        self.skip_download = skip_download
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


    # Detect visual separator lines that should force a text newline.
    def _compute_newline_separators(
        self,
        page: pdfplumber.page.Page,
    ) -> list[float]:
        page_height = float(page.height)
        separators: list[float] = []

        for line in page.lines:
            linewidth = line.get("linewidth")
            if linewidth is None:
                continue

            if abs(float(linewidth) - NEWLINE_LINEWIDTH) > 1e-6:
                continue

            line_top = self._line_pos_from_top(line, page_height)
            if line_top is None:
                continue

            separators.append(float(line_top))

        return sorted(separators)

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
            if line_top > page_height * 0.25 and abs(line['width'] - 51.024) < 1.0:
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
        prev_base_y1 = None

        count_top_cutoff = 0
        count_bottom_cutoff = 0
        count_matrix = 0
        count_annotation = 0
        count_invisible = 0
        
        for char in page.chars:
            top, bottom = self._char_bounds_from_top(char, page_height)
            if top is None or bottom is None:
                continue
            if bottom_cutoff is not None and top > bottom_cutoff:
                count_bottom_cutoff += 1
                continue
            if top_cutoff is not None and bottom < top_cutoff:
                count_top_cutoff += 1
                continue

            # remove non horizontal text
            matrix = char.get("matrix")
            if matrix[1] != 0.0 or matrix[2] != 0.0:
                if not self.notified_about_matrix:
                    self.logger.info(f"Filtering out characters with non-horizontal matrix (e.g. rotated text). Page: {page}.")
                    self.notified_about_matrix = True
                count_matrix += 1
                continue

            # Remove ####-ing invisible characters:
            non_stroking_color = char.get("non_stroking_color")
            stroking_color = char.get("stroking_color")
            if len(non_stroking_color) == 1 and non_stroking_color[0] == 0.0 and len(stroking_color) != 1:
                count_invisible += 1
                continue

            
            size = char.get("size")
            base_y0 = char.get("y0")
            base_y1 = char.get("y1")
            
            if (
                size is not None
                and base_y0 is not None
                and base_y1 is not None
                and size < ANNOTATION_MAX_SIZE
                and prev_base_y0 is not None
                and prev_base_y1 is not None
                and (
                    # Superscript check: current baseline (y0) is significantly higher than previous
                    (float(base_y0) - float(prev_base_y0)) >= ANNOTATION_BASELINE_DELTA
                    or 
                    # Subscript check: current top (y1) is significantly lower than previous
                    (float(prev_base_y1) - float(base_y1)) >= ANNOTATION_BASELINE_DELTA
                )
            ):
                count_annotation += 1
                continue

            filtered_chars.append(char)
            
            # Update previous positions for the next iteration
            if base_y0 is not None:
                prev_base_y0 = float(base_y0)
            if base_y1 is not None:
                prev_base_y1 = float(base_y1)
                
        print(f"Page {page.page_number}: Filtered {count_top_cutoff} chars above top cutoff, {count_bottom_cutoff} chars below bottom cutoff, {count_matrix} chars with non-horizontal matrix, {count_annotation} chars likely annotations, {count_invisible} invisible chars. Kept {len(filtered_chars)} chars.")
        return filtered_chars


    # Remove table/figure blocks between heading and source line.
    def _filter_table_blocks(
        self,
        page: pdfplumber.page.Page,
        chars: list[dict],
        in_table_block: bool = False,   # persisted across pages
    ) -> tuple[list[dict], bool]:
        """
        Remove table/figure blocks between a heading and its source line.

        Returns:
            kept_chars,
            updated_in_table_block
        """
        page_height = float(page.height)

        # Vertical spans of each table, from top to bottom, for current page
        tables_spans: list[list[int]] = []

        page_lines = page.extract_text_lines()

        is_in_table_block = in_table_block


        table_start = None if not is_in_table_block else float(0.0)
        table_end = None
        for line in page_lines:
            if TABLE_START_RE.match(line["text"]):
                table_start = line["top"]
                is_in_table_block = True
            if SOURCE_RE.match(line["text"]):
                table_end = line["top"]

            if table_start is not None and table_end is not None:
                tables_spans.append([table_start,table_end])
                table_start = None
                table_end = None
                is_in_table_block = False

        if table_start:
            table_end = page_height
            tables_spans.append([table_start,table_end])
            is_in_table_block = True

        filtered_chars = []
        for char in chars:
            is_in_table = False
            for table in tables_spans:
                if char['top'] >= table[0] and char['top'] <= table[1]:
                    is_in_table = True
                    break
            if not is_in_table:
                filtered_chars.append(char)

        return filtered_chars, is_in_table_block
    

    # Merge consecutive characters by font size into text blocks.
    def _merge_by_size(
        self,
        chars: list[dict],
        page_index: int,
        page_width: float,
        source_name: str | None,
        newline_separators: list[float] | None = None,
        state: tuple[float | None, bool | None, list[str], int | None] | None = None,
    ) -> tuple[list[tuple[str, int]], tuple[float | None, bool | None, list[str], int | None]]:
        def non_space_len(value: str) -> int:
            return sum(1 for ch in value if not ch.isspace())

        def is_bold(fontname: str | None) -> bool:
            if not fontname:
                return False
            return "bold" in fontname.lower()

        def is_letter(value: str) -> bool:
            return bool(value) and value.isalpha()

        passed_separators: set[float] = set()

        merged_lines: list[tuple[str, int]] = []
        if state:
            current_size, current_bold, current_text, last_page_index = state
        else:
            current_size = None
            current_bold = None
            current_text = []
            last_page_index = page_index
        current_chars: list[dict] = []
        fragment_prev_char = None
        prev_char = None
        last_stream_char = None
        for char_index, char in enumerate(chars):
            char_top = char.get("top")

            if char_top is not None and newline_separators:
                for separator_y in newline_separators:
                    if separator_y in passed_separators:
                        continue

                    # Text moved below the separator line.
                    if float(char_top) > separator_y:
                        if current_text:
                            line_text = "".join(current_text).strip()
                            if line_text:
                                merged_lines.append((line_text, last_page_index or page_index))
                            
                            current_text = []
                            current_chars = []
                            fragment_prev_char = None

                        # Force logical newline.
                        merged_lines.append(("", last_page_index or page_index))

                        passed_separators.add(separator_y)

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
                # if size_changed:
                #     print(
                #         "Page {page} char {idx}: size {old:.3f} -> {new:.3f}"
                #         .format(
                #             page=page_index,
                #             idx=char_index,
                #             old=current_size if current_size is not None else -1.0,
                #             new=size if size is not None else -1.0,
                #         )
                #     )
                # if bold_changed:
                #     print(
                #         "Page {page} char {idx}: bold {old} -> {new}"
                #         .format(page=page_index, idx=char_index, old=current_bold, new=bold_for_break)
                #     )
                if current_text:
                    line_text = "".join(current_text).strip()
                    if line_text:
                        if CITATION_STOP_RE.search(line_text):
                            merged_lines.append((CITATION_STOP_MARKER, last_page_index or page_index))
                        else:
                            merged_lines.append((line_text, last_page_index or page_index))
                       
                    current_text = []
                    current_chars = []
                    fragment_prev_char = None
                merged_lines.append(("", last_page_index or page_index))
                fragment_prev_char = last_stream_char
                current_text.append(text)
                current_chars.append(char)
                current_size = size
                current_bold = bold_for_break
                prev_char = None
                last_stream_char = char
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

            if not current_text:
                fragment_prev_char = last_stream_char
            current_text.append(text)
            current_chars.append(char)
            prev_char = char
            last_stream_char = char

        if current_text:
            line_text = "".join(current_text).strip()
            if line_text and CITATION_STOP_RE.search(line_text):
                merged_lines.append((CITATION_STOP_MARKER, last_page_index or page_index))
                current_text = []
                current_chars = []
                
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
            try:
                detector = Detector(line, quiet=True)
                langs = detector.languages
                lang = langs[0].code if langs else None
            except Exception:
                kept.append(line)
                continue
            probs = ",".join(f"{item.code}:{item.confidence:.3f}" for item in langs)
            if lang == "en" and langs[0].confidence >= 90.0:
                removed.append((page_no, line, probs))
                continue
            kept.append(line)

        if removed:
            with open(log_path, "a", encoding="utf-8") as f:
                for page_no, line, probs in removed:
                    f.write(f"{pdf_path.parent.name}\tpage {page_no}\t{probs}\t{line}\n")
                f.write("\n")

        return kept
    
    def _extract_text_from_pdf(self, pdf_path: Path) -> str:
        with pdfplumber.open(pdf_path) as pdf:
            all_pages_output: list[tuple[str, int]] = []
            stop_all = False
            merge_state: tuple[float | None, bool | None, list[str], int | None] | None = None
            in_table_block = False

            for page_index, page in enumerate(pdf.pages, start=1):
                if stop_all:
                    break

                if page_index == 1:
                    print(page.extract_text_lines(return_chars=False))
                
                # First, we remove any characters below annotation line and above header line
                # Also, we delete any non horizontal text and super/subscript
                filtered_chars = self._filter_chars(page)

                                
                if page_index == 7:
                    with Path("test_aa.json").open("w", encoding="utf-8") as f:
                        json.dump(page.chars, f, ensure_ascii=False, indent=2)

                # Now we remove table/figures, with persistant state between pages
                filtered_chars, in_table_block = self._filter_table_blocks(page, filtered_chars, in_table_block)

                # For the thick, 2.0 width lines separating english from polish
                newline_separators = self._compute_newline_separators(page)




                # Merge chars into whole text fragments
                # detects size changes, font changes to separate by fragments
                merged_lines, merge_state = self._merge_by_size(
                    filtered_chars,
                    page_index,
                    float(page.width),
                    pdf_path.name,
                    newline_separators,
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
            final_text = "\n".join(cleaned_output)
            final_text = re.sub(r"\n{3,}", "\n\n", final_text)

            return final_text
        
    def _extract_text_from_pdf_ocr(self, pdf_path: Path) -> str:
        from paddleocr import PaddleOCRVL
        try:
            pipeline = PaddleOCRVL(vl_rec_backend="vllm-server", vl_rec_server_url="http://localhost:8118/v1")

            output = pipeline.predict(input=pdf_path)

            pages_res = list(output)

            output = pipeline.restructure_pages(pages_res, merge_tables=True, relevel_titles=True, concatenate_pages=True)

            # save output to file
            with Path("test_paddle_output.json").open("w", encoding="utf-8") as f:
                for res in output:
                    json.dump(res, f, ensure_ascii=False, indent=2)

        except Exception as exc:
            self.logger.warning("PaddleOCR extraction failed for %s: %s", pdf_path, exc)
            return ""

    def extract(self, data, limit: int = None, start_at_index: int = 0):
        self.logger.info("Phase 1: downloading PDFs")
        articles = data[:limit+start_at_index] if limit else data
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
                extracted_text = self._extract_text_from_pdf_ocr(pdf_path)
            except Exception as exc:
                self.logger.warning("Failed to extract PDF text for %s: %s", article_dir, exc)
                continue
            extraction_times.append(time.perf_counter() - phase_start)

            (article_dir / "extracted_text.txt").write_text(extracted_text.strip() + "\n", encoding="utf-8")

            metadata_path = article_dir / "metadata.json"
            metadata_text = json.dumps(article.to_json(), ensure_ascii=False, indent=2)
            metadata_path.write_text(metadata_text + "\n", encoding="utf-8")