import argparse
import logging
from pathlib import Path
from crawlers.crawler_factory import CrawlerFactory
from extractors.extractor_factory import ExtractorFactory
import json

logger = logging.getLogger("ScraperMain")


def process_site(site: str, skip_extraction: bool, limit: int, workers: int, output_dir: str = None):
    logger.info(f"Starting crawl for {site}...")
    data = CrawlerFactory.create_crawler(site).crawl(workers=workers, limit=limit)
    logger.info(f"Finished crawling for {site}.")

    if data is None:
        logger.error(f"No data returned from crawler for {site}. Skipping writing and extraction.")
        return
    
    path_to_write = Path(output_dir) if output_dir else Path(".")
    with open(path_to_write / f"{site}_data.json", "w", encoding="utf-8") as f:
        articles_json = json.dumps([article.to_json() for article in data], ensure_ascii=False, indent=2)
        f.write(articles_json)

    logger.info(f"Data for {site} written to {site}_data.json in {output_dir if output_dir else 'current directory'}")

    if not skip_extraction:
        logger.info(f"Starting extraction for {site}...")
        ExtractorFactory.create_extractor(site).extract(data, limit)
        logger.info(f"Finished extraction for {site}.")


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Scraper command template")
	parser.add_argument("--output-dir", help="Write results to this directory instead of stdout")
	parser.add_argument("--site", choices=("czasopisma", "sejm", "journals", "pressto", "all"), default="czasopisma", help="Site to scrape")
	parser.add_argument("--skip-extraction", action="store_true", help="Skip extraction phase")
	parser.add_argument("--limit", type=int, help="Optional limit for issues to process")
	parser.add_argument("--workers", type=int, default=1, help="Number of worker threads to use for crawling")
	return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
    args = parse_args()

    sites_to_process = None
    if args.site == "all":
        sites_to_process = ["czasopisma", "sejm", "journals", "pressto"]
    else:
        sites_to_process = [args.site]

    logger.info("Number of worker threads: %d", args.workers)
    logger.info("Following sites will be processed: %s", args.site)
    if args.skip_extraction:
        logger.info("Extraction phase will be skipped.")
        if args.limit:
            logger.warning("Limit argument will be ignored since extraction is skipped.")
    elif args.limit:
        logger.info("Limit for issues to process: %d", args.limit)
    logger.info("Output directory: %s", args.output_dir if args.output_dir else "stdout")

    for site in sites_to_process:
        process_site(site, args.skip_extraction, args.limit, args.workers, args.output_dir)

    logger.info("All done!")
