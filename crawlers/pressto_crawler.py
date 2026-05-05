
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup

from utils.issue import ArticleData

from .crawler import Crawler

PAGE_COUNT = 3

class PresstoCrawler(Crawler):
    def __init__(self):
        super().__init__()
        self.base_url = "https://pressto.amu.edu.pl/index.php/rpeis/"
        self.journal_name = "Ruch Prawniczy, Ekonomiczny i Socjologiczny"


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
        issue_name = soup.find("div", class_="container page-issue").find("h1").text.strip()
        article_divs = soup.find_all("div", class_="article-summary")
        article_divs = article_divs[1:]  # Skip the first which are not articles (TOC)

        articles = []
        for article_div in article_divs:
            article_title = article_div.find("div", class_="article-summary-title").find("a", href=True).text.strip()
            article_url = article_div.find("div", class_="article-summary-title").find("a", href=True)["href"]
            buttons = article_div.find("div", class_="article-summary-galleys").find_all("a", href=True)
  
            language = "Other"
            for button in buttons:
                if button.text.strip() == "PDF":
                    language = "Polish"
                    break
                
            articles.append(ArticleData(title=article_title, url=article_url, issue=issue_name, journal=self.journal_name, language=language))

        self.logger.info(f"Extracted {len(articles)} articles from issue {issue_name}")
        return articles
    

    def _get_pdf_and_license(self, article_url):
        self.logger.info(f"Fetching article page {article_url} for PDF link and license info fetching...")
        html = self._get_html_from_url(article_url)
        if not html:
            self.logger.error(f"Failed to fetch article page {article_url} for PDF link and license info fetching.")
            return None, None
        
        soup = BeautifulSoup(html, "html.parser")
        pdf_link_divs = soup.find_all("div", class_="article-details-galley")

        for link in pdf_link_divs:
            link_a = link.find("a", href=True)
            if link_a and link_a.text.strip() == "PDF":
                pdf_subpage_link = link_a["href"]
                break

        license_info = soup.find("div", class_="item copyright")
        license = license_info.find_all("p")[1].find("a", href=True).text.strip() if license_info and len(license_info.find_all("p")) > 1 and license_info.find_all("p")[1].find("a", href=True) else None

        self.logger.info(f"Fetching PDF subpage {pdf_subpage_link} for article {article_url}...")
        html = self._get_html_from_url(pdf_subpage_link)
        if not html:
            self.logger.error(f"Failed to fetch PDF subpage {pdf_subpage_link} for article {article_url}.")
            return None, None
        
        soup = BeautifulSoup(html, "html.parser")
        pdf_link = soup.find("div", class_="pdf-download-button").find("a", href=True)["href"]

        return pdf_link, license


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
            future_to_article = {executor.submit(self._get_pdf_and_license, article.url): article for article in filtered_articles}

            for future in as_completed(future_to_article):
                article = future_to_article[future]
                try:
                    pdf_link, license_info = future.result()
                    article.pdf_url = pdf_link if pdf_link else article.url
                    article.license = license_info if license_info else "Unknown"
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
