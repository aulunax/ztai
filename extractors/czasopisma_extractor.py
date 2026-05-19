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
                    # Drop tiny raised numbers, they are usually footnote markers.
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
    # Treat any small-text block in the bottom half as footnotes/annotations.
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
    # Remove inline citation/page numbers injected by PDF text flow.
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


def _normalize_midword_caps(text: str) -> str:
    word_re = re.compile(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]{2,}")

    def repl(match: re.Match[str]) -> str:
        word = match.group(0)
        if word.isupper():
            return word
        return word[0] + word[1:].lower()

    return word_re.sub(repl, text)


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

    def is_section_heading(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        if heading_re.match(stripped):
            return True

        if len(stripped) > 90:
            return False
        if re.search(r"://|@", stripped):
            return False
        if re.search(r"[\.;!]$", stripped):
            return False
        if re.search(r"[,:;]\s", stripped):
            return False

        words = re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż0-9]+", stripped)
        if not words or len(words) > 8:
            return False

        return bool(stripped[0].isupper() or stripped[0].isdigit())

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

        if is_section_heading(line):
            out.append(line)
            out.append("")
            idx += 1

            body_parts: list[str] = []
            while idx < len(source_lines):
                current = source_lines[idx]
                if not current:
                    idx += 1
                    continue
                if is_section_heading(current):
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


def _extract_article_content_from_pdf_path(pdf_path: Path) -> tuple[str, str | None]:
    with pdfplumber.open(str(pdf_path)) as pdf:
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

    article_text = _normalize_midword_caps(_compact_numbered_sections(_render_lines_as_text(cleaned_lines)))
    return article_text, licence_info


class CzasopismaExtractor(Extractor):
    def __init__(self, output_dir: str | None = None, skip_download: bool = False):
        super().__init__()
        self.output_dir = Path(output_dir) if output_dir else Path(".")
        self.skip_download = skip_download

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

    def extract(self, data, limit: int = None):
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
                extracted_text, pdf_licence = _extract_article_content_from_pdf_path(pdf_path)
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