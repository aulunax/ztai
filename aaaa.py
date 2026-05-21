from pathlib import Path

ROOT_DIR = Path("output/data/pressto")
OUTPUT_FILE = Path("single_char_lines.txt")

results = []

for txt_file in ROOT_DIR.rglob("extracted_text.txt"):
    parent_dir = txt_file.parent.name

    with txt_file.open("r", encoding="utf-8", errors="ignore") as f:
        for line_number, line in enumerate(f, start=1):
            stripped = line.strip()

            # non-empty and exactly 1 character
            if len(stripped) == 1:
                results.append(
                    f"{parent_dir} | line {line_number} | {repr(stripped)}"
                )

with OUTPUT_FILE.open("w", encoding="utf-8") as f:
    f.write("\n".join(results))

print(f"Found {len(results)} matching lines.")
print(f"Saved to: {OUTPUT_FILE}")