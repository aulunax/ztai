#!/usr/bin/env python3
import argparse
import os
import shutil
from datetime import datetime

def find_site_dirs(base_dir: str) -> list[str]:
    if not os.path.isdir(base_dir):
        return []
    return [
        os.path.join(base_dir, name)
        for name in os.listdir(base_dir)
        if os.path.isdir(os.path.join(base_dir, name))
    ]

def iter_issue_dirs(site_dir: str) -> list[str]:
    return [
        os.path.join(site_dir, name)
        for name in os.listdir(site_dir)
        if os.path.isdir(os.path.join(site_dir, name))
    ]

def should_delete(issue_dir: str, min_bytes: int) -> bool:
    text_path = os.path.join(issue_dir, "extracted_text.txt")
    if not os.path.isfile(text_path):
        return False
    try:
        return os.path.getsize(text_path) < min_bytes
    except OSError:
        return False

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Remove issue folders with extracted_text.txt smaller than the minimum size"
        )
    )
    parser.add_argument(
        "--base-dir",
        default=os.path.join("data", "data"),
        help="Base directory containing site folders (default: data/data)",
    )
    parser.add_argument(
        "--min-bytes",
        type=int,
        default=2 * 1024,
        help="Minimum extracted_text.txt size in bytes (default: 2048)",
    )
    parser.add_argument(
        "--log-file",
        default="deleted_dirs.log",
        help="Log file path (default: deleted_dirs.log)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List deletions without removing directories",
    )
    args = parser.parse_args()

    base_dir = args.base_dir
    min_bytes = args.min_bytes

    deleted_dirs: list[str] = []
    for site_dir in find_site_dirs(base_dir):
        for issue_dir in iter_issue_dirs(site_dir):
            if should_delete(issue_dir, min_bytes):
                deleted_dirs.append(issue_dir)
                if not args.dry_run:
                    shutil.rmtree(issue_dir, ignore_errors=True)

    if deleted_dirs:
        timestamp = datetime.utcnow().isoformat() + "Z"
        with open(args.log_file, "a", encoding="utf-8") as log_file:
            log_file.write(f"# {timestamp}\n")
            for path in deleted_dirs:
                log_file.write(path + "\n")
    if args.dry_run:
        print(f"Dry run: {len(deleted_dirs)} directories would be deleted")
    else:
        print(f"Deleted {len(deleted_dirs)} directories")

if __name__ == "__main__":
    main()
