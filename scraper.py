import argparse
import logging
import time
from pathlib import Path
from crawlers.crawler_factory import CrawlerFactory
from extractors.extractor_factory import ExtractorFactory
import json

logger = logging.getLogger("ScraperMain")


def process_site(
    site: str,
    skip_extraction: bool,
    limit: int,
    workers: int,
    output_dir: str = None,
    sejm_issues_file: str = None,
):
    site_start = time.perf_counter()
    logger.info(f"Starting crawl for {site}...")
    crawl_start = time.perf_counter()
    data = CrawlerFactory.create_crawler(site, sejm_issues_file=sejm_issues_file).crawl(
        workers=workers,
        limit=limit,
    )
    crawl_duration = time.perf_counter() - crawl_start
    logger.info("Finished crawling for %s in %.2fs.", site, crawl_duration)

    if data is None:
        logger.error(f"No data returned from crawler for {site}. Skipping writing and extraction.")
        return
    
    path_to_write = Path(output_dir) if output_dir else Path(".")
    with open(path_to_write / f"{site}_data.json", "w", encoding="utf-8") as f:
        articles_json = json.dumps([article.to_json() for article in data], ensure_ascii=False, indent=2)
        f.write(articles_json)

    logger.info(
        "Data for %s written to %s.",
        site,
        path_to_write / f"{site}_data.json",
    )

    if not skip_extraction:
        logger.info(f"Starting extraction for {site}...")
        extract_start = time.perf_counter()
        ExtractorFactory.create_extractor(site).extract(data, limit)
        extract_duration = time.perf_counter() - extract_start
        logger.info("Finished extraction for %s in %.2fs.", site, extract_duration)

    site_duration = time.perf_counter() - site_start
    logger.info("Total time for %s: %.2fs.", site, site_duration)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scraper command template")
    parser.add_argument("--output-dir", help="Write results to this directory instead of stdout")
    parser.add_argument("--site", choices=("czasopisma", "sejm", "journals", "pressto", "all"), default="czasopisma", help="Site to scrape")
    parser.add_argument(
        "--sejm-issues-file",
        help="Path to JSON file with Sejm issue URLs. If provided and exists, step 1 is skipped.",
    )
    parser.add_argument("--skip-extraction", action="store_true", help="Skip extraction phase")
    parser.add_argument("--limit", type=int, help="Optional limit for issues to process")
    parser.add_argument("--workers", type=int, default=1, help="Number of worker threads to use for crawling")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
    args = parse_args()
    run_start = time.perf_counter()

    sites_to_process = None
    if args.site == "all":
        sites_to_process = ["czasopisma", "journals", "pressto", "sejm"]
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

    sejm_issues_file = args.sejm_issues_file
    if "sejm" in sites_to_process and not sejm_issues_file:
        output_root = Path(args.output_dir) if args.output_dir else Path(".")
        sejm_issues_file = str(output_root / "sejm_issues.json")
    if sejm_issues_file:
        logger.info("Sejm issues cache file: %s", sejm_issues_file)

    for site in sites_to_process:
        process_site(
            site,
            args.skip_extraction,
            args.limit,
            args.workers,
            args.output_dir,
            sejm_issues_file if site == "sejm" else None,
        )

    run_duration = time.perf_counter() - run_start
    logger.info("Total run time: %.2fs.", run_duration)
    logger.info("All done!")
