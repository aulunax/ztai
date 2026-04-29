from __future__ import annotations

import argparse
from collections import Counter
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median

import pdfplumber


@dataclass
class Line:
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


def extract_page_lines(page: pdfplumber.page.Page, page_no: int) -> list[Line]:
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
                    "tokens": [
                        {
                            "text": text,
                            "size": size,
                            "top": top,
                        }
                    ],
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

    out: list[Line] = []
    for line in raw_lines:
        tokens = list(line.get("tokens", []))

        # Remove likely superscript-style numeric annotation markers.
        if tokens:
            numeric_re = re.compile(r"^\d{1,3}$")
            non_numeric_sizes = [
                float(tok["size"])
                for tok in tokens
                if not numeric_re.match(str(tok["text"])) and float(tok["size"]) > 0
            ]
            all_sizes = [float(tok["size"]) for tok in tokens if float(tok["size"]) > 0]
            base_size = median(non_numeric_sizes) if non_numeric_sizes else (median(all_sizes) if all_sizes else 0.0)

            non_numeric_tops = [
                float(tok["top"])
                for tok in tokens
                if not numeric_re.match(str(tok["text"]))
            ]
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
            Line(
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


def is_running_header_or_footer(line: Line, body_size: float) -> bool:
    t = line.text
    near_top = line.top < line.page_height * 0.12
    near_bottom = line.top > line.page_height * 0.90
    small = line.size <= body_size - 1.8
    header_signature = bool(re.search(r"\[\d+\]", t) or re.search(r"\b\d+\b", t[:24]))

    if near_bottom and small:
        return True
    if near_top and (small or header_signature):
        return True
    return False


def is_bottom_annotation(line: Line, body_size: float) -> bool:
    # Typical footnote/reference text is smaller and appears in lower page region.
    if line.top < line.page_height * 0.45:
        return False
    if line.size > body_size - 0.9:
        return False
    if re.match(r"^\d+\s+", line.text):
        return True
    if re.search(r"\b(?:Dz\.\s*U\.|OSNC|Legalis|Lex|Glosa|Monitor|Przegląd)\b", line.text):
        return True
    return True


def drop_first_page_front_matter(lines: list[Line], body_size: float) -> list[Line]:
    if not lines:
        return lines

    # Find first centered uppercase title line (article title starts here).
    title_start = None
    for i, ln in enumerate(lines):
        if not (ln.size >= body_size + 0.8 and ln.is_centered and len(ln.text) >= 18):
            continue

        alpha = sum(1 for c in ln.text if c.isalpha())
        upper = sum(1 for c in ln.text if c.isupper())
        uppercase_ratio = upper / max(1, alpha)
        if uppercase_ratio > 0.72:
            title_start = i
            break

    if title_start is None:
        return lines
    return lines[title_start:]


def find_cutoff_after_last_numbered_section(lines: list[Line], body_size: float) -> int:
    numbered = []
    for idx, ln in enumerate(lines):
        m = re.match(r"^(\d+)\.\s+\S", ln.text)
        if not m:
            continue
        if abs(ln.size - body_size) <= 0.4 or ln.size >= body_size:
            numbered.append((idx, int(m.group(1))))

    if not numbered:
        return len(lines)

    max_num = max(num for _, num in numbered)
    last_idx = max(idx for idx, num in numbered if num == max_num)

    # After the last numbered section starts, cut when a new heading block appears:
    # - non-numbered
    # - centered or visibly separated by a large vertical gap
    # - heading-like font (>= body text)
    chars_after_last_heading = 0
    for i in range(last_idx + 1, len(lines)):
        ln = lines[i]
        chars_after_last_heading += len(ln.text)

        # Do not cut too early: we still want full content of the final numbered section.
        if i - last_idx < 8 or chars_after_last_heading < 900:
            continue

        if re.match(r"^\d+\.\s+\S", ln.text):
            continue
        if ln.size < body_size - 0.1:
            continue

        prev = lines[i - 1]
        gap = ln.top - prev.bottom
        heading_like = ln.is_centered or gap >= body_size * 1.6
        if not heading_like:
            continue

        # Strong signal: immediate continuation with another heading-like line.
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if nxt and nxt.size >= body_size - 0.3 and (nxt.is_centered or (nxt.top - ln.bottom) < body_size * 1.2):
            return i

        # Or this heading is followed by smaller abstract/meta body.
        if nxt and nxt.size <= body_size - 1.0:
            return i

        # Fallback signal: a large visual gap before this non-numbered body-size line.
        if gap >= body_size * 1.9:
            return i

    return len(lines)


def normalize_hyphenation_and_spacing(text: str) -> str:
    # Join line-break hyphenation: "pełno-\nmocnik" -> "pełnomocnik".
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    # Join forced line wraps inside paragraphs.
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    # Remove inline numeric artifacts between lowercase words, e.g. "z nim 1 w".
    text = re.sub(
        r"(?<=[a-ząćęłńóśźż])(?:\s+\d{1,3}){1,3}(?=\s+[a-ząćęłńóśźż])",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Remove split-word numeric artifacts, e.g. "modyfika2 cja".
    text = re.sub(r"(?<=[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])\d{1,2}(?=[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])", "", text)
    # Remove leftover spaces before punctuation after annotation cleanup.
    text = re.sub(r"\s+([,\.;:!?\)])", r"\1", text)
    # Remove spaces after opening brackets.
    text = re.sub(r"([\(\[])\s+", r"\1", text)
    # Normalize whitespace.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_lines_as_text(lines: list[Line]) -> str:
    out: list[str] = []
    last_page = None
    last_bottom = None
    for ln in lines:
        if last_page is not None and ln.page_no != last_page:
            out.append("\n")
            last_bottom = None

        if last_bottom is not None and (ln.top - last_bottom) > ln.size * 1.35:
            out.append("\n")

        out.append(ln.text + "\n")
        last_page = ln.page_no
        last_bottom = ln.bottom

    return normalize_hyphenation_and_spacing("".join(out))


def compact_numbered_sections(text: str) -> str:
    heading_re = re.compile(r"^\d+\.\s+\S")
    src = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    i = 0

    while i < len(src):
        line = src[i]

        if not line:
            if out and out[-1] != "":
                out.append("")
            i += 1
            continue

        if heading_re.match(line):
            out.append(line)
            i += 1

            body_parts: list[str] = []
            while i < len(src):
                cur = src[i]
                if not cur:
                    i += 1
                    continue
                if heading_re.match(cur):
                    break
                body_parts.append(cur)
                i += 1

            body = re.sub(r"\s+", " ", " ".join(body_parts)).strip()
            if body:
                out.append(body)

            if out and out[-1] != "":
                out.append("")
            continue

        out.append(line)
        i += 1

    # Normalize empty lines and trim trailing blanks.
    normalized: list[str] = []
    for ln in out:
        if ln == "" and (not normalized or normalized[-1] == ""):
            continue
        normalized.append(ln)

    while normalized and normalized[-1] == "":
        normalized.pop()

    return "\n".join(normalized)


def extract_article_text(pdf_path: Path) -> tuple[str, list[str]]:
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = [extract_page_lines(page, i + 1) for i, page in enumerate(pdf.pages)]

    all_lines = [ln for page in pages for ln in page]

    # Estimate body size from numbered section headers first; fallback to dominant text size.
    heading_sizes = [
        ln.size
        for ln in all_lines
        if re.match(r"^\d+\.\s+\S", ln.text)
        and 9.0 <= ln.size <= 14.0
    ]
    if heading_sizes:
        body_size = float(median(heading_sizes))
    else:
        bucket = Counter(round(ln.size * 2) / 2 for ln in all_lines if 8.5 <= ln.size <= 13.0)
        body_size = float(max(bucket.items(), key=lambda kv: (kv[1], kv[0]))[0]) if bucket else 11.0

    cleaned_pages: list[list[Line]] = []
    for i, page_lines in enumerate(pages):
        lines = [ln for ln in page_lines if not is_running_header_or_footer(ln, body_size)]
        if i == 0:
            lines = drop_first_page_front_matter(lines, body_size)
        lines = [ln for ln in lines if not is_bottom_annotation(ln, body_size)]
        cleaned_pages.append(lines)

    cleaned_lines = [ln for page in cleaned_pages for ln in page]
    cutoff = find_cutoff_after_last_numbered_section(cleaned_lines, body_size)
    cleaned_lines = cleaned_lines[:cutoff]

    # Safety net: drop any dangling heading-like line at the very end.
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

    full_text = compact_numbered_sections(render_lines_as_text(cleaned_lines))
    page_texts: list[str] = []
    for page_no in sorted({ln.page_no for ln in cleaned_lines}):
        page_lines = [ln for ln in cleaned_lines if ln.page_no == page_no]
        page_texts.append(render_lines_as_text(page_lines))

    return full_text, page_texts


def normalize_for_compare(text: str) -> str:
    text = text.casefold()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_for_compare(a), normalize_for_compare(b)).ratio()


def main() -> None:
    parser = argparse.ArgumentParser(description="Layout-aware extraction with pdfplumber")
    parser.add_argument("--pdf", default="/home/kamilony/ztai/aaa.pdf", help="Path to PDF")
    parser.add_argument("--out", default="/home/kamilony/ztai/aaa_extracted.txt", help="Output text path")
    parser.add_argument("--expected-page1", default="/home/kamilony/ztai/page1.txt")
    parser.add_argument("--expected-page2", default="/home/kamilony/ztai/page2.txt")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    out_path = Path(args.out)

    text, page_texts = extract_article_text(pdf_path)
    out_path.write_text(text, encoding="utf-8")

    print(f"Saved extracted text to: {out_path}")
    print(f"Extracted chars: {len(text)}")

    exp1 = Path(args.expected_page1).read_text(encoding="utf-8")
    exp2 = Path(args.expected_page2).read_text(encoding="utf-8")

    p1 = page_texts[0] if len(page_texts) >= 1 else ""
    p2 = page_texts[1] if len(page_texts) >= 2 else ""

    sim1 = similarity(p1, exp1)
    sim2 = similarity(p2, exp2)
    print(f"Page1 similarity vs expected: {sim1:.4f}")
    print(f"Page2 similarity vs expected: {sim2:.4f}")


if __name__ == "__main__":
    main()