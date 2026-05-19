from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import shutil
from time import sleep
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.support.ui import Select
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from utils.issue import ArticleData

from .crawler import Crawler

# As declared here: https://ps.sejm.gov.pl/Journal.nsf/PS.xsp?view=1&lang=PL
LICENSE_SEJM="Creative Commons Uznanie Autorstwa 3.0 PL (CC BY 3.0 PL)."


class SejmCrawler(Crawler):
    def __init__(self, issues_cache_path: str | Path | None = None):
        super().__init__()
        self.base_url = "https://ps.sejm.gov.pl/Journal.nsf/PS.xsp"
        self.journal_name = "Przegląd Sejmowy"
        self.issues_cache_path = Path(issues_cache_path) if issues_cache_path else None

    def _setup_firefox_driver(self) -> webdriver.Firefox:
        """Initializes the Selenium WebDriver with smart binary resolution."""
        options = webdriver.FirefoxOptions()
        options.add_argument("--headless")
        options.set_preference("intl.accept_languages", "pl-PL,pl")

        # Auto-detect Firefox binary (useful for Linux/Snap environments)
        candidates = [
            "/snap/firefox/current/usr/lib/firefox/firefox",
            "firefox-esr",
            "firefox",
        ]
        
        for candidate in candidates:
            resolved = shutil.which(candidate) if not os.path.isabs(candidate) else candidate
            if resolved and os.path.exists(resolved):
                options.binary_location = resolved
                break

        driver = webdriver.Firefox(options=options)
        driver.set_window_size(1920, 1080)
        driver.set_page_load_timeout(10)
        return driver

    def _get_issues_pages_urls(self) -> list[str]:
        """
        Replaces the BeautifulSoup logic. 
        Uses Selenium to interact with the dropdown and extract all issues.
        """
        
        driver = self._setup_firefox_driver()
        wait = WebDriverWait(driver, 10)
        issue_urls = []
        max_retries = 3

        try:
            self.logger.info(f"Opening Sejm archive: {self.base_url}?view=12&lang=PL")

            # Aura: first load always triggers HTTP 500, so we do be loading it twice
            driver.get(f"{self.base_url}?view=12&lang=PL")
            sleep(0.5)
            driver.get(f"{self.base_url}?view=12&lang=PL")

            # Wait for the dropdown to load
            select_el = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "select[id$=':cbYear']"))
            select = Select(select_el)
            
            # Extract all year options into memory (safe from driver restarts)
            year_options = [(opt.get_attribute("value"), opt.text.strip()) for opt in select.options if opt.get_attribute("value")]
            self.logger.info(f"Found {len(year_options)} selectable years")

            for year_value, year_label in year_options:
                for attempt in range(max_retries):
                    try:
                        self.logger.info(f"Extracting issues for year: {year_label} (Attempt {attempt + 1}/{max_retries})")
                        current_year_urls = []

                        # Reselect the dropdown 
                        current_select_el = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "select[id$=':cbYear']"))
                        Select(current_select_el).select_by_value(year_value)

                        # Wait for the data table to update
                        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "div[id$=':rok'] table tbody tr")) > 0)
                        
                        # Parse the rows
                        rows = driver.find_elements(By.CSS_SELECTOR, "div[id$=':rok'] table tbody tr")
                        for row in rows:
                            links = row.find_elements(By.CSS_SELECTOR, "a[href]")
                            if not links:
                                continue

                            issue_page_href = None
                            pdf_href = None
                            issue_name = ""

                            for link in links:
                                href = link.get_attribute("href")
                                href_lower = href.lower() if href else ""
                                
                                if issue_page_href is None and "documentid=" in href_lower:
                                    issue_page_href = href
                                    issue_name = issue_name or " ".join(link.text.split())

                            chosen_href = issue_page_href or pdf_href or links[0].get_attribute("href")
                            if not chosen_href:
                                continue
                            
                            issue_name = issue_name or " ".join(row.text.split())
                            issue_url = urljoin(driver.current_url, chosen_href)
                            current_year_urls.append(issue_url)
                            
                        issue_urls.extend(current_year_urls)
                        break # Success, break out of the retry loop

                    except Exception as e:
                        self.logger.warning(f"Error extracting year {year_label} on attempt {attempt + 1}: {e}")
                        
                        if attempt == max_retries - 1:
                            self.logger.error(f"Failed to extract year {year_label} after {max_retries} attempts. Skipping.")
                        else:
                            self.logger.info("Murdering is occuring...")
                            
                            try:
                                driver.quit()
                            except Exception as quit_err:
                                self.logger.debug(f"Murdering failed, reason: {quit_err}")
                                raise Exception("Murdering failed, cannot continue")
                            
                            self.logger.info("Murdering successful, reviving driver...")

                            # wait just to be sure
                            sleep(1)
                            
                            driver = self._setup_firefox_driver()
                            wait = WebDriverWait(driver, 10)
                            
                            # Aura: first load always triggers HTTP 500, so we do be loading it twice
                            driver.get(f"{self.base_url}?view=12&lang=PL")
                            sleep(0.5)
                            driver.get(f"{self.base_url}?view=12&lang=PL")

        finally:
            # Ensure the final driver is closed when everything is done
            try:
                driver.quit()
            except:
                pass

        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for item in issue_urls:
            if item not in seen:
                seen.add(item)
                deduped.append(item)

        return deduped

    def _load_issue_urls(self) -> list[str]:
        if not self.issues_cache_path:
            return []
        try:
            with open(self.issues_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                self.logger.warning("Issue cache file %s is not a list. Ignoring.", self.issues_cache_path)
                return []
            return [str(item) for item in data if item]
        except FileNotFoundError:
            return []
        except Exception as exc:
            self.logger.warning("Failed to load issue cache from %s: %s", self.issues_cache_path, exc)
            return []

    def _save_issue_urls(self, issues_pages: list[str]):
        if not self.issues_cache_path:
            return
        self.issues_cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.issues_cache_path, "w", encoding="utf-8") as f:
            json.dump(issues_pages, f, ensure_ascii=False, indent=2)
        self.logger.info("Issue URLs saved to %s", self.issues_cache_path)
    
    def _get_articles_from_issue_page(self, html: str):
        soup = BeautifulSoup(html, "html.parser")
        issue_name = soup.find("h2").text.strip()
        article_rows= soup.find_all("tr")

        articles = []
        for article_row in article_rows:
            article_row_cols = article_row.find_all("td")
            if len(article_row_cols) < 4:
                self.logger.warning(f"Skipping malformed article row in issue {issue_name}: not enough columns")
                continue
            article_title = article_row_cols[1].find("a").text.strip()
            article_url = article_row_cols[1].find("a", href=True)["href"]
            pdf_link = article_row_cols[3].find("a", href=True)
            language = "Polish"
                
            articles.append(ArticleData(title=article_title, url=article_url, pdf_url=pdf_link["href"] if pdf_link else None, issue=issue_name, journal=self.journal_name, language=language, license=LICENSE_SEJM))

        self.logger.info(f"Extracted {len(articles)} articles from issue {issue_name}")
        return articles

    def crawl(self, workers: int = 1, limit: int = None):
        issues_pages = self._load_issue_urls()
        if issues_pages:
            self.logger.info("Loaded %d issue URLs from %s", len(issues_pages), self.issues_cache_path)
        else:
            # Step 1: Get all issue URLs from the archive using Selenium
            self.logger.info("Step 1: Fetching issue URLs via Selenium...")
            issues_pages = self._get_issues_pages_urls()
            self.logger.info("Total issues found: %d", len(issues_pages))
            self._save_issue_urls(issues_pages)

        if limit is not None:
            issues_pages = issues_pages[:limit]
            self.logger.info(f"Limiting to {len(issues_pages)} issues due to limit argument.")

        # Step 2: Everything else, since this website has to be different of course, why not

        issue_urls_not_pdf = []
        all_articles = []

        for issue_url in issues_pages:
            if not issue_url.endswith(".pdf"):
                issue_urls_not_pdf.append(issue_url)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_url = {executor.submit(self._fetch_issue, url, True): url for url in issue_urls_not_pdf}

            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    articles = future.result()
                    all_articles.extend(articles)
                except Exception as e:
                    self.logger.error(f"Issue {url} generated an exception: {e}")

        self.logger.info(f"All initial issues: {len(issues_pages)}")
        self.logger.info(f"Total issues with only PDF links (no direct URL): {len(issue_urls_not_pdf)}")
        self.logger.info(f"Total issues with articles: {len(issues_pages)- len(issue_urls_not_pdf)}")
        self.logger.info(f"Total articles extracted: {len(all_articles)}")


        return all_articles
