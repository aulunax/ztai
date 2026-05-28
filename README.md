# Web Scraper

This project contains a web scraping script `scraper.py` configured to crawl and extract data from various sites.

## Prerequisites

- You need to install a GPU-appropriate build of `paddleocr` for your hardware and CUDA version (https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html#manual-install-inference-engine-and-paddleocr).
- `polyglot` requires `pyicu`, and `pyicu` usually needs ICU libraries installed on your OS.
- For OCR extraction, have the PaddleOCR GenAI vLLM server running in the background with Docker:

```bash
docker run -it --rm --gpus all -p 8119:8119 \
  -v $(pwd)/vllm_config.yml:/tmp/vllm_config.yml:ro \
  ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddleocr-genai-vllm-server:latest-nvidia-gpu-offline \
  paddleocr genai_server --model_name PaddleOCR-VL-1.5-0.9B --host 0.0.0.0 --port 8119 --backend vllm --backend_config /tmp/vllm_config.yml
```

## Usage

You can run the scraper using Python from the command line:

```bash
python scraper.py [OPTIONS]
```

### Options

- `--site {czasopisma,sejm,journals,pressto,all}`
  Specifies the site to scrape.
  - `czasopisma` (default)
  - `sejm`
  - `journals`
  - `pressto`
  - `all`

- `--output-dir OUTPUT_DIR`
  The directory where the results will be written (as JSON files). If not provided, it writes to the current directory.

- `--sejm-issues-file PATH`
  Path to JSON file with Sejm issue URLs. If provided and exists, step 1 is skipped.

- `--workers WORKERS`
  The number of worker threads to use for crawling in parallel. Default is `1`.

- `--skip-download`
  Skip PDF download phase for all extractors.

- `--skip-text-extraction`
  Skip text extraction phase in extractors (PDFs still downloaded unless `--skip-download`).

- `--skip-extraction`
  A flag that skips the data extraction phase (no PDF interaction at all). If selected, it will only crawl the site and save basic data.

- `--skip-crawl-load`
  Skip crawling and load cached data from `<output-dir>/<site>_data.json`.

- `--issue-limit LIMIT`
  Optional limit for issues to process.

- `--extract-limit LIMIT`
  Optional limit for articles to process in extraction phase.

- `--start-at-index INDEX`
  Index of the first article to process in extraction phase (default: 0).

### Examples

**Scrape a single site ("pressto") with 10 worker threads, skipping extraction, and output to the "output" directory:**
```bash
python scraper.py --site pressto --workers 10 --skip-extraction --output-dir output
```

**Scrape all sites with the default settings (1 worker thread, with extraction):**
```bash
python scraper.py --site all --output-dir output
```

**Scrape the default site ("czasopisma") limiting extraction to 5 issues:**
```bash
python scraper.py --issue-limit 5 --output-dir output
```

**Resume extraction from a specific article index, without re-downloading PDFs:**
```bash
python scraper.py --site journals --start-at-index 200 --skip-download --output-dir output
```

**Reuse cached crawl data and only run extraction for up to 100 articles:**
```bash
python scraper.py --site pressto --skip-crawl-load --extract-limit 100 --output-dir output
```

## Other scripts

### `cleanup_small_extractions.py`

Removes issue folders where `extracted_text.txt` is smaller than a minimum size. Useful for cleaning bad OCR outputs.

```bash
python cleanup_small_extractions.py --base-dir data/data --min-bytes 2048 --dry-run
```

Options:
- `--base-dir`: base directory containing site folders (default: `data/data`).
- `--min-bytes`: minimum allowed size for `extracted_text.txt` (default: `2048`).
- `--log-file`: log path for deleted directories (default: `deleted_dirs.log`).
- `--dry-run`: list deletions without removing directories.

### `strip_extracted_annotations.py`

Strips LaTeX-style inline annotations and metadata (ORCID, DOI, emails) from `extracted_text.txt` files and logs changes.

```bash
python strip_extracted_annotations.py --base-dir data/data --dry-run
```

Options:
- `--base-dir`: base directory containing site folders (default: `data/data`).
- `--log-file`: log path for removed snippets (default: `annotation_strip.log`).
- `--dry-run`: scan and report changes without writing files.
