# Web Scraper

This project contains a web scraping script `scraper.py` configured to crawl and extract data from various sites.

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
  - `all` (scrapes all the available sites sequentially)

- `--output-dir OUTPUT_DIR`
  The directory where the results will be written (as JSON files). If not provided, it writes to the current directory.

- `--workers WORKERS`
  The number of worker threads to use for crawling in parallel. Default is `1`.

- `--skip-extraction`
  A flag that skips the data extraction phase (No interaction with .pdf at all). If selected, it will only crawl the site and save basic data.

- `--limit LIMIT`
  An optional integer limit for the number of issues/articles to process.

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
python scraper.py --limit 5 --output-dir output
```
