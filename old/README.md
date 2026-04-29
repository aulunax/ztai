## Usage

Czasopisma, journals and pressto have very similar workflow when scraping.
Running the command below and replacing works well enough for these:
```sh
python3 [script] --workers 16 --format json --output [output-file] --pretty
```

### Czasopisma examples

Full scrape for Czasopisma:
```sh
python3 czasopisma/scrape_czasopisma.py --workers 16 --format json --output czasopisma/articles-2.json --pretty --extract-pdf-content --article-export-dir czasopisma/data --skip-english-articles
```

### Sejm
Sejm website on the other hand, requires Selenium for the first step, which is getting all the links to the pages with particular issues.
The URL of pages for each documents are randomized IDs, and the website uses ajax or smth to update the archive list based on the year,
so scraping required first using selenium to gather all the required links, and then putting those links through the actual data scraper.