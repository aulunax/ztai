
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
        self.journal_name = "Zeszyty Prawnicze"
    

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
        sections = soup.find_all("div", class_="section")
        article_section = None
        for section in sections:
            if section.find("h2").text.strip().lower() == "artykuły": 
                article_section = section
                break
        article_divs = article_section.find_all("div", class_="card")

        articles = []
        for article_div in article_divs:
            article_title = article_div.find("h3").text.strip()
            article_url = article_div.find("a", href=True)["href"]
            btn_text = article_div.find("a", class_="btnDownload").text.strip() if article_div.find("a", class_="btnDownload") else None
            language = "Polish" if btn_text == "PDF" else "Other"

            articles.append(ArticleData(title=article_title, url=article_url, issue=issue_name, language=language, journal=self.journal_name))

        self.logger.info(f"Extracted {len(articles)} articles from issue {issue_name}")
        return articles


    def _get_pdf_link(self, article_url):
        self.logger.info(f"Fetching article page {article_url} for PDF link fetching...")
        html = self._get_html_from_url(article_url)
        if not html:
            self.logger.error(f"Failed to fetch article page {article_url} for PDF link fetching.")
            return None
        
        soup = BeautifulSoup(html, "html.parser")
        pdf_subpage_link = soup.find("a", class_="btnDownload", href=True)["href"]

        self.logger.info(f"Fetching PDF subpage {pdf_subpage_link} for article {article_url}...")
        html = self._get_html_from_url(pdf_subpage_link)
        if not html:
            self.logger.error(f"Failed to fetch PDF subpage {pdf_subpage_link} for article {article_url}.")
            return None
        
        soup = BeautifulSoup(html, "html.parser")
        pdf_link = soup.find("a", class_="download", href=True)["href"]
        return pdf_link


    def crawl(self, workers: int = 1, limit: int = None):
        # Step 1: Get all issue URLs from the archive pages
        issues_pages = []
        for page in range(1, PAGE_COUNT + 1):
            self.logger.info(f"Fetching archive page {page} for Czasopisma...")
            html = self._get_html_from_url(f"{self.base_url}issue/archive/{page}")
            if html:
                issues_pages.extend(self._get_issues_pages_urls(html, page))

        self.logger.info(f"Total issues found: {len(issues_pages)}")

        if limit is not None:
            issues_pages = issues_pages[:limit]
            self.logger.info(f"Limiting to {len(issues_pages)} issues due to limit argument.")

        # Step 2: Fetch articles from each issue page in parallel
        all_articles = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_url = {executor.submit(self._fetch_issue, url): url for url in issues_pages}

            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    articles = future.result()
                    all_articles.extend(articles)
                except Exception as e:
                    self.logger.error(f"Issue {url} generated an exception: {e}")

        self.logger.info(f"Total articles extracted: {len(all_articles)}")
        filtered_articles = [article for article in all_articles if article.language == "Polish"]
        self.logger.info(f"Total articles excluding non-polish/no-pdf-links: {len(filtered_articles)}")

        # Step 3: Get PDF links for articles
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_article = {executor.submit(self._get_pdf_link, article.url): article for article in filtered_articles}

            for future in as_completed(future_to_article):
                article = future_to_article[future]
                try:
                    pdf_link = future.result()
                    if pdf_link:
                        article.pdf_url = pdf_link
                except Exception as e:
                    self.logger.error(f"Article {article.url} generated an exception: {e}")

        total_with_pdf = len([article for article in filtered_articles if article.pdf_url])
        total_with_license = len([article for article in filtered_articles if article.license and article.license != "Unknown"])
        total_with_pdf_and_license = len([article for article in filtered_articles if article.pdf_url and article.license and article.license != "Unknown"])

        self.logger.info(f"Total valid articles: {len(filtered_articles)}")
        self.logger.info(f"Total articles with PDF links: {total_with_pdf}")
        self.logger.info(f"Total articles with license info: {total_with_license}")
        self.logger.info(f"Total articles with both PDF links and license info: {total_with_pdf_and_license}")

        return filtered_articles
