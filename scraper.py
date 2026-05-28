import argparse
import json
import logging
import time
from pathlib import Path
from crawlers.crawler_factory import CrawlerFactory
from extractors.extractor_factory import ExtractorFactory
from utils.issue import ArticleData

logger = logging.getLogger("ScraperMain")


def process_site(
    site: str,
    skip_extraction: bool,
    skip_text_extraction: bool,
    issue_limit: int,
    extract_limit: int,
    workers: int,
    output_dir: str = None,
    sejm_issues_file: str = None,
    skip_download: bool = False,
    skip_crawl_load: bool = False,
    start_at_index: int = 0,
):
    site_start = time.perf_counter()
    path_to_write = Path(output_dir) if output_dir else Path(".")
    path_to_write.mkdir(parents=True, exist_ok=True)

    data = None
    if skip_crawl_load:
        data_path = path_to_write / f"{site}_data.json"
        try:
            with open(data_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            data = [
                ArticleData(
                    title=item.get("title"),
                    issue=item.get("issue"),
                    journal=item.get("journal_name"),
                    url=item.get("article_url"),
                    pdf_url=item.get("pdf_url"),
                    license=item.get("license"),
                    language=item.get("language"),
                )
                for item in payload
                if isinstance(item, dict)
            ]
            logger.info("Loaded %d items from %s", len(data), data_path)
        except FileNotFoundError:
            logger.error("Missing cached crawl data at %s", data_path)
            return
        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON in %s: %s", data_path, exc)
            return
    else:
        logger.info(f"Starting crawl for {site}...")
        crawl_start = time.perf_counter()
        data = CrawlerFactory.create_crawler(site, sejm_issues_file=sejm_issues_file).crawl(
            workers=workers,
            limit=issue_limit,
        )
        crawl_duration = time.perf_counter() - crawl_start
        logger.info("Finished crawling for %s in %.2fs.", site, crawl_duration)

    if data is None:
        logger.error(f"No data returned from crawler for {site}. Skipping writing and extraction.")
        return
    
    if not skip_crawl_load:
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
        ExtractorFactory.create_extractor(
            site,
            output_dir=output_dir,
            skip_download=skip_download,
            skip_text_extraction=skip_text_extraction,
        ).extract(data, extract_limit, start_at_index=start_at_index)
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
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip PDF download phase for all extractors.",
    )
    parser.add_argument(
        "--skip-text-extraction",
        action="store_true",
        help="Skip text extraction phase in extractors (PDFs still downloaded unless --skip-download).",
    )
    parser.add_argument("--skip-extraction", action="store_true", help="Skip extraction phase")
    parser.add_argument(
        "--skip-crawl-load",
        action="store_true",
        help="Skip crawling and load cached data from <output-dir>/<site>_data.json",
    )
    parser.add_argument("--issue-limit", type=int, help="Optional limit for issues to process")
    parser.add_argument("--extract-limit", type=int, help="Optional limit for articles to process in extraction phase")
    parser.add_argument("--start-at-index", type=int, default=0, help="Index of the first article to process in extraction phase")
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
        if args.issue_limit:
            logger.warning("Issue limit argument will be ignored since extraction is skipped.")
    if args.skip_crawl_load:
        logger.info("Crawling phase will be skipped (loading cached data).")
    elif args.issue_limit:
        logger.info("Limit for issues to process: %d", args.issue_limit)
    if args.skip_download:
        logger.info("Download phase will be skipped.")
    if args.skip_text_extraction:
        logger.info("Text extraction phase will be skipped.")
    if args.extract_limit:
        logger.info("Limit for articles to process: %d", args.extract_limit)
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
            args.skip_text_extraction,
            args.issue_limit,
            args.extract_limit,
            args.workers,
            args.output_dir,
            sejm_issues_file if site == "sejm" else None,
            args.skip_download,
            args.skip_crawl_load,
            args.start_at_index,
        )

    run_duration = time.perf_counter() - run_start
    logger.info("Total run time: %.2fs.", run_duration)
    logger.info("All done!")
