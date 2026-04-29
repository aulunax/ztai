
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry
from bs4 import BeautifulSoup

from utils.issue import ArticleData

from .crawler import Crawler

PAGE_COUNT = 2


class CzasopismaCrawler(Crawler):
    def __init__(self):
        super().__init__()
        self.base_url = "https://czasopisma.uksw.edu.pl/index.php/zp/"
    
    def _get_issues_pages_urls(self, html: str, page: int):
        soup = BeautifulSoup(html, "html.parser")
        urls = set()
        for link in soup.find_all("a", href=True):
            if "/issue/view/" in link["href"]:
                urls.add(link["href"])
        self.logger.info(f"Found {len(urls)} issues on page {page}")
        return urls
    
    def _get_articles_from_issue_page(self, html: str):
        soup = BeautifulSoup(html, "html.parser")
        issue_name = soup.find("main").find("h2").text.strip()
        article_divs = soup.find_all("div", class_="card")
        article_divs = article_divs[1:-1]  # Skip the first and last one which are not articles

        articles = []
        for article_div in article_divs:
            article_title = article_div.find("h3").text.strip()
            article_url = article_div.find("a", href=True)["href"]
            articles.append(ArticleData(title=article_title, url=article_url, issue=issue_name))

        self.logger.info(f"Extracted {len(articles)} articles from issue {issue_name}")
        return articles

    def _fetch_issue(self, issue_url, session):
        self.logger.info(f"Fetching issue page {issue_url} for Czasopisma...")
        html = self._get_html_from_url(issue_url, session)
        if html:
            return self._get_articles_from_issue_page(html)
        return []

    def crawl(self, workers: int = 1):
        session = self._build_session()
        issues_pages = []
        for page in range(1, PAGE_COUNT + 1):
            self.logger.info(f"Fetching archive page {page} for Czasopisma...")
            html = self._get_html_from_url(f"{self.base_url}issue/archive/{page}", session)
            if html:
                issues_pages.extend(self._get_issues_pages_urls(html, page))

        self.logger.info(f"Total issues found: {len(issues_pages)}")

        all_articles = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_url = {executor.submit(self._fetch_issue, url, session): url for url in issues_pages}
            
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    articles = future.result()
                    all_articles.extend(articles)
                except Exception as e:
                    self.logger.error(f"Issue {url} generated an exception: {e}")

        self.logger.info(f"Total articles extracted: {len(all_articles)}")

        return all_articles
