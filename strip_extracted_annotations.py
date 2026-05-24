#!/usr/bin/env python3
import argparse
from datetime import datetime
from pathlib import Path
import re

def process_file(
    path: Path,
    inline_patterns: list[re.Pattern],
    dry_run: bool,
) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    removed: list[str] = []

    for pattern in inline_patterns:
        removed.extend(match.group(0) for match in pattern.finditer(text))
        text = pattern.sub("", text)

    updated = text
    if not removed:
        return []

    if not dry_run:
        path.write_text(updated, encoding="utf-8")
    return removed

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Remove LaTeX-style annotations and metadata lines from extracted_text.txt files"
        )
    )
    parser.add_argument(
        "--base-dir",
        default=Path("data") / "data",
        type=Path,
        help="Base directory containing site folders (default: data/data)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report changes without writing files",
    )
    parser.add_argument(
        "--log-file",
        default=Path("annotation_strip.log"),
        type=Path,
        help="Log file path (default: annotation_strip.log)",
    )
    args = parser.parse_args()

    inline_patterns = [
        re.compile(r"\s*\$\s*\^\{[^}]*\}\s*\$\s*"),
        re.compile(r"\s*\$[^$]*\$\s*"),
        re.compile(
            r"\bORCID\s*:\s*\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bDOI\s*:\s*10\.\d{4,9}/[-._;()/:A-Za-z0-9]+",
            re.IGNORECASE,
        ),
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ]
    base_dir = args.base_dir

    changed = 0
    scanned = 0

    timestamp = datetime.utcnow().isoformat() + "Z"
    for text_path in base_dir.glob("*/*/extracted_text.txt"):
        scanned += 1
        removed = process_file(text_path, inline_patterns, args.dry_run)
        if removed:
            changed += 1
            with args.log_file.open("a", encoding="utf-8") as log_file:
                log_file.write(f"# {timestamp}\t{text_path}\n")
                for item in removed:
                    log_file.write(item.replace("\n", "\\n") + "\n")
                log_file.write("\n")

    if args.dry_run:
        print(f"Dry run: {changed} of {scanned} files would be updated")
    else:
        print(f"Updated {changed} of {scanned} files")

if __name__ == "__main__":
    main()
