#!/usr/bin/env python3
"""Format and preview extracted PDF text from scraper JSON output."""

from __future__ import annotations

import argparse
import json
import re
import textwrap
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview formatted extracted article text from JSON output.",
    )
    parser.add_argument(
        "--input",
        default="czasopisma/articles.json",
        help="Path to scraper JSON output file.",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=5,
        help="Maximum number of records to print/export.",
    )
    parser.add_argument(
        "--article-url-contains",
        default=None,
        help="Only include records whose article_url contains this substring.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Only include records with this article_language value (e.g. Polish, English).",
    )
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Include records with missing article_text.",
    )
    parser.add_argument(
        "--full-text",
        action="store_true",
        help="Print full article text (default prints trimmed preview).",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=5000,
        help="Maximum number of article_text characters when not using --full-text.",
    )
    parser.add_argument(
        "--wrap-width",
        type=int,
        default=100,
        help="Wrap width for formatted text output.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional directory to write one formatted .txt file per record.",
    )
    return parser.parse_args()


def load_records(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file_obj:
        payload = json.load(file_obj)
    if not isinstance(payload, list):
        raise ValueError("Input JSON must be a list of records.")
    return [record for record in payload if isinstance(record, dict)]


def clean_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return cleaned[:120] if cleaned else "record"


def wrap_text(raw_text: str, width: int) -> str:
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [part.strip() for part in normalized.split("\n\n")]

    wrapped_parts: list[str] = []
    for part in paragraphs:
        if not part:
            continue
        one_line = re.sub(r"\s+", " ", part).strip()
        wrapped_parts.append(textwrap.fill(one_line, width=width))

    return "\n\n".join(wrapped_parts)


def format_record(
    record: dict[str, Any],
    index: int,
    wrap_width: int,
    full_text: bool,
    max_chars: int,
) -> str:
    article_url = str(record.get("article_url") or "")
    issue_url = str(record.get("issue_url") or "")
    journal_name = str(record.get("journal_name") or "")
    journal_name_with_issue = str(record.get("journal_name_with_issue") or "")
    article_language = str(record.get("article_language") or "")
    pdf_download_url = str(record.get("pdf_download_url") or "")
    licence_info = str(record.get("licence_info") or "")

    article_text_raw = str(record.get("article_text") or "")
    if not full_text and max_chars >= 0 and len(article_text_raw) > max_chars:
        article_text_raw = article_text_raw[:max_chars].rstrip() + "\n\n[...trimmed...]"

    article_text = wrap_text(article_text_raw, width=wrap_width) if article_text_raw else ""

    lines = [
        f"Record #{index}",
        f"Article URL: {article_url}",
        f"Issue URL: {issue_url}",
        f"Journal: {journal_name}",
        f"Journal + issue: {journal_name_with_issue}",
        f"Language: {article_language}",
        f"PDF URL: {pdf_download_url}",
        f"Licence info: {licence_info}",
        "",
        "Extracted text:",
        article_text if article_text else "[empty]",
    ]
    return "\n".join(lines).strip() + "\n"


def should_include(
    record: dict[str, Any],
    article_url_contains: str | None,
    language: str | None,
    include_empty: bool,
) -> bool:
    text_value = str(record.get("article_text") or "")
    if not include_empty and not text_value.strip():
        return False

    if article_url_contains:
        url = str(record.get("article_url") or "")
        if article_url_contains.lower() not in url.lower():
            return False

    if language:
        record_language = str(record.get("article_language") or "")
        if record_language.lower() != language.lower():
            return False

    return True


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    records = load_records(input_path)

    filtered = [
        record
        for record in records
        if should_include(record, args.article_url_contains, args.language, args.include_empty)
    ]

    if args.max_records >= 0:
        filtered = filtered[: args.max_records]

    if not filtered:
        print("No matching records.")
        return 0

    output_dir_path: Path | None = None
    if args.output_dir:
        output_dir_path = Path(args.output_dir)
        output_dir_path.mkdir(parents=True, exist_ok=True)

    for idx, record in enumerate(filtered, start=1):
        formatted = format_record(
            record=record,
            index=idx,
            wrap_width=max(args.wrap_width, 20),
            full_text=args.full_text,
            max_chars=args.max_chars,
        )

        print("=" * 100)
        print(formatted, end="")

        if output_dir_path is not None:
            article_url = str(record.get("article_url") or "")
            slug_source = article_url.rsplit("/", maxsplit=1)[-1] or f"record_{idx}"
            file_name = f"{idx:04d}_{clean_filename(slug_source)}.txt"
            (output_dir_path / file_name).write_text(formatted, encoding="utf-8")

    if output_dir_path is not None:
        print("=" * 100)
        print(f"Wrote {len(filtered)} formatted files to: {output_dir_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
