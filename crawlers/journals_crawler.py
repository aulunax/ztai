from concurrent.futures import ThreadPoolExecutor, as_completed
import re

from bs4 import BeautifulSoup

from utils.issue import ArticleData

from .crawler import Crawler

PAGE_COUNT = 2

class JournalsCrawler(Crawler):
    def __init__(self):
        super().__init__()
        self.base_url = "https://journals.umcs.pl/sil/"
        self.journal_name = "Studia Iuridica Lublinensia"


    def _get_issues_pages_urls(self, html: str, page: int):
        soup = BeautifulSoup(html, "html.parser")
        urls = set()
        for link in soup.find_all("a", href=True):
            if "/issue/view/" in link["href"]:
                urls.add(link["href"] + "/showToc")
        self.logger.info(f"Found {len(urls)} issues on page {page}")
        return urls
    
    def _get_articles_from_issue_page(self, html: str):
        soup = BeautifulSoup(html, "html.parser")
        issue_name = soup.find("h2").text.strip()
        article_tables = soup.find_all("table", class_="tocArticle")
        article_tables = article_tables[1:]  # Skip the first article (TOC)

        articles = []
        for article_table in article_tables:
            article_title = article_table.find("div", class_="tocTitle").find("a", href=True).text.strip() if article_table.find("div", class_="tocTitle") and article_table.find("div", class_="tocTitle").find("a", href=True) else None
            article_url = article_table.find("div", class_="tocTitle").find("a", href=True)["href"] if article_table.find("div", class_="tocTitle") and article_table.find("div", class_="tocTitle").find("a", href=True) else None
            pdf_refs = article_table.find_all("a", class_="file")
            pdf_texts = [pdf_ref.text.strip() for pdf_ref in pdf_refs]
            language = "Polish" if "PDF (Język Polski)" in pdf_texts else "Other"

            # Fail check: Sometimes the article title is not a link, but the pdf link contains the article link
            if not article_url and not article_title:
                article_title = article_table.find("div", class_="tocTitle").text.strip() if article_table.find("div", class_="tocTitle") else None
                article_url = article_table.find("a", href=True)["href"] if article_table.find("a", href=True) else None
                article_url = article_url.rsplit("/", 1)[0] if article_url else None
            
            if not article_url or not article_title:
                self.logger.warning(f"No article URL or title found for article in issue '{issue_name}'. Skipping this article.")
                continue

            articles.append(ArticleData(title=article_title, url=article_url, issue=issue_name, language=language, journal=self.journal_name))

        self.logger.info(f"Extracted {len(articles)} articles from issue {issue_name}")
        return articles
    
    def _fetch_issue(self, issue_url):
        self.logger.info(f"Fetching issue page {issue_url} for Journals...")
        html = self._get_html_from_url(issue_url)
        if html:
            return self._get_articles_from_issue_page(html)
        return []
    
    def _get_pdf_and_license(self, article_url):
        self.logger.info(f"Fetching article page {article_url} for PDF link and license info fetching...")
        html = self._get_html_from_url(article_url)
        if not html:
            self.logger.error(f"Failed to fetch article page {article_url} for PDF link and license info fetching.")
            return None, None
        
        soup = BeautifulSoup(html, "html.parser")
        pdf_link = soup.find("a", class_="file", href=True, text="PDF (Język Polski)")["href"]
        pdf_link = pdf_link.replace("/view/", "/download/")
        license_info = soup.find_all("a", rel="license")
        license = license_info[1].text.strip() if len(license_info) > 1 else "Unknown"
        if len(license_info) == 1:
            self.logger.warning(f"Only one license link found for article {article_url}. License info may be incomplete.")

        return pdf_link, license

    def crawl(self, workers: int = 1, limit: int = None):
        # Step 1: Get all issue URLs from the archive pages
        issues_pages = []
        for page in range(1, PAGE_COUNT + 1):
            self.logger.info(f"Fetching archive page {page} for Journals...")
            html = self._get_html_from_url(f"{self.base_url}issue/archive?issuesPage={page}")
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

        # Step 3: Get PDF links and license info for articles
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_article = {executor.submit(self._get_pdf_and_license, article.url): article for article in filtered_articles}

            for future in as_completed(future_to_article):
                article = future_to_article[future]
                try:
                    pdf_link, license_info = future.result()
                    article.url = pdf_link if pdf_link else article.url
                    article.license = license_info if license_info else "Unknown"
                except Exception as e:
                    self.logger.error(f"Article {article.url} generated an exception: {e}")

        return filtered_articles
