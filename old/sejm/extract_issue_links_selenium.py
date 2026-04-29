from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from typing import Iterable
from urllib.parse import urljoin

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait

LOGGER = logging.getLogger("sejm_issue_links_selenium")


@dataclass
class IssueLinkRecord:
    selected_year_value: str
    selected_year_label: str
    issue_name: str
    issue_url: str
    pdf_url: str | None = None


class IssueLinkExtractor:
    def __init__(
        self,
        archive_url: str,
        timeout: int = 30,
        headless: bool = True,
        manual_wait_seconds: int = 0,
        firefox_binary: str | None = None,
    ) -> None:
        self.archive_url = archive_url
        self.timeout = timeout
        self.headless = headless
        self.manual_wait_seconds = max(manual_wait_seconds, 0)
        self.firefox_binary = firefox_binary
        self.driver: webdriver.Firefox | None = None

    @staticmethod
    def _resolve_firefox_binary(user_provided: str | None = None) -> str | None:
        if user_provided:
            return user_provided

        candidates = [
            "/snap/firefox/current/usr/lib/firefox/firefox",
            "firefox-esr",
            "firefox",
        ]

        for candidate in candidates:
            if os.path.isabs(candidate):
                if os.path.exists(candidate):
                    return candidate
                continue

            resolved = shutil.which(candidate)
            if resolved:
                return resolved

        return None

    def _build_driver(self) -> webdriver.Firefox:
        options = webdriver.FirefoxOptions()
        if self.headless:
            options.add_argument("--headless")
        options.set_preference("intl.accept_languages", "pl-PL,pl")

        firefox_binary = self._resolve_firefox_binary(self.firefox_binary)
        if firefox_binary:
            options.binary_location = firefox_binary
            LOGGER.info("Using Firefox binary: %s", firefox_binary)
        else:
            LOGGER.warning("Firefox binary was not auto-detected; WebDriver will use its default resolution.")

        driver = webdriver.Firefox(options=options)
        driver.set_window_size(1920, 1080)
        driver.set_page_load_timeout(max(self.timeout, 10))
        return driver

    def _wait(self) -> WebDriverWait:
        if self.driver is None:
            raise RuntimeError("WebDriver is not initialized")
        return WebDriverWait(self.driver, self.timeout)

    @staticmethod
    def _normalize_text(text: str | None) -> str:
        if not text:
            return ""
        return " ".join(text.split())

    @staticmethod
    def _dedupe_keep_order(items: Iterable[IssueLinkRecord]) -> list[IssueLinkRecord]:
        seen: set[tuple[str, str | None]] = set()
        out: list[IssueLinkRecord] = []
        for item in items:
            key = (item.issue_url, item.pdf_url)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _open_archive(self) -> None:
        assert self.driver is not None

        LOGGER.info("Opening archive: %s", self.archive_url)
        self.driver.get(self.archive_url)

        if self.manual_wait_seconds > 0:
            LOGGER.info("Waiting %ds for manual challenge solve if needed.", self.manual_wait_seconds)
            time.sleep(self.manual_wait_seconds)

        self._wait().until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "select[id$=':cbYear']")) > 0)

    def _extract_year_options(self) -> list[tuple[str, str]]:
        assert self.driver is not None

        select_el = self._wait().until(lambda d: d.find_element(By.CSS_SELECTOR, "select[id$=':cbYear']"))
        select = Select(select_el)

        options: list[tuple[str, str]] = []
        for option in select.options:
            value = self._normalize_text(option.get_attribute("value"))
            label = self._normalize_text(option.text)
            if not value:
                continue
            options.append((value, label or value))

        LOGGER.info("Found %d selectable years", len(options))
        return options

    def _select_year(self, year_value: str) -> None:
        assert self.driver is not None

        select_el = self._wait().until(lambda d: d.find_element(By.CSS_SELECTOR, "select[id$=':cbYear']"))
        Select(select_el).select_by_value(year_value)

        # Prefer explicit marker that changes on this page.
        try:
            self._wait().until(
                lambda d: (d.find_element(By.ID, "renderedYear").get_attribute("value") or "").strip() == year_value
            )
        except TimeoutException:
            # Fallback for variants where marker is absent.
            pass

        self._wait().until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "div[id$=':rok'] table tbody tr")) > 0)

    def _collect_current_year_issues(self, year_value: str, year_label: str) -> list[IssueLinkRecord]:
        assert self.driver is not None

        rows = self.driver.find_elements(By.CSS_SELECTOR, "div[id$=':rok'] table tbody tr")
        out: list[IssueLinkRecord] = []

        for row in rows:
            links = row.find_elements(By.CSS_SELECTOR, "a[href]")
            if not links:
                continue

            issue_page_href: str | None = None
            pdf_href: str | None = None
            issue_name = ""

            for link in links:
                href = self._normalize_text(link.get_attribute("href"))
                if not href:
                    continue

                href_lower = href.lower()
                if issue_page_href is None and "documentid=" in href_lower:
                    issue_page_href = href
                    issue_name = issue_name or self._normalize_text(link.text)

                if pdf_href is None and (
                    "liczopen" in href_lower
                    or href_lower.endswith(".pdf")
                    or ".pdf?" in href_lower
                ):
                    pdf_href = href

            chosen_href = issue_page_href or pdf_href or self._normalize_text(links[0].get_attribute("href"))
            if not chosen_href:
                continue

            issue_name = issue_name or self._normalize_text(row.text)
            if not issue_name:
                issue_name = self._normalize_text(links[0].text)
            if not issue_name:
                continue

            issue_url = urljoin(self.driver.current_url, chosen_href)
            pdf_url = urljoin(self.driver.current_url, pdf_href) if pdf_href else None

            out.append(
                IssueLinkRecord(
                    selected_year_value=year_value,
                    selected_year_label=year_label,
                    issue_name=issue_name,
                    issue_url=issue_url,
                    pdf_url=pdf_url,
                )
            )

        LOGGER.info("Year %s: collected %d issues", year_value, len(out))
        return out

    def run(self, max_years: int | None = None) -> list[IssueLinkRecord]:
        self.driver = self._build_driver()

        try:
            self._open_archive()
            year_options = self._extract_year_options()
            if max_years is not None:
                year_options = year_options[: max(max_years, 0)]

            all_items: list[IssueLinkRecord] = []
            for year_value, year_label in year_options:
                try:
                    self._select_year(year_value)
                    all_items.extend(self._collect_current_year_issues(year_value, year_label))
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("Failed year %s (%s): %s", year_label, year_value, exc)

            deduped = self._dedupe_keep_order(all_items)
            LOGGER.info("Done. Total unique issues: %d", len(deduped))
            return deduped
        finally:
            if self.driver is not None:
                self.driver.quit()
                self.driver = None


def write_output(items: list[IssueLinkRecord], output_path: str | None, output_format: str, pretty: bool) -> None:
    if output_format == "json":
        payload = [asdict(item) for item in items]
        text = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
        if output_path:
            with open(output_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(text)
            LOGGER.info("Wrote %d records to %s", len(items), output_path)
        else:
            print(text)
        return

    fieldnames = ["selected_year_value", "selected_year_label", "issue_name", "issue_url", "pdf_url"]
    if output_path:
        with open(output_path, "w", encoding="utf-8", newline="") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
            writer.writeheader()
            for item in items:
                writer.writerow(asdict(item))
        LOGGER.info("Wrote %d records to %s", len(items), output_path)
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            writer.writerow(asdict(item))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract issue links+names from all year options in Sejm archive selector."
    )
    parser.add_argument(
        "--archive-url",
        default="https://ps.sejm.gov.pl/Journal.nsf/PS.xsp?view=12&lang=PL",
        help="Archive URL with year selector.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Selenium wait timeout in seconds.",
    )
    parser.add_argument(
        "--manual-wait-seconds",
        type=int,
        default=0,
        help="Optional extra wait after page open (manual anti-bot solve window).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Firefox in headless mode.",
    )
    parser.add_argument(
        "--firefox-binary",
        default=None,
        help="Optional explicit path to Firefox binary.",
    )
    parser.add_argument(
        "--max-years",
        type=int,
        default=None,
        help="Optional limit of processed year options.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="Output format.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path; if omitted prints to stdout.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty JSON output.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    extractor = IssueLinkExtractor(
        archive_url=args.archive_url,
        timeout=args.timeout,
        headless=args.headless,
        manual_wait_seconds=args.manual_wait_seconds,
        firefox_binary=args.firefox_binary,
    )

    try:
        items = extractor.run(max_years=args.max_years)
    except WebDriverException as exc:
        LOGGER.error("WebDriver error: %s", exc)
        return 2

    write_output(items, args.output, args.format, args.pretty)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
