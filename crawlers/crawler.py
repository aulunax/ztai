from abc import ABC, abstractmethod
import logging
from time import sleep

import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry


class Crawler(ABC):
    def __init__(self):
        self.base_url = ""
        self.session = self._build_session()
        self.logger = logging.getLogger(self.__class__.__name__)

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)        
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session
    
    def _get_html_from_url(self, url: str):
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            self.logger.error(f"Error fetching from {self.base_url}: {e}")
            return None
        
    def _fetch_issue(self, issue_url, sejm=False):
        self.logger.info(f"Fetching issue page {issue_url}...")

        if sejm:
            # WE love sejm
            self._get_html_from_url(issue_url)
            sleep(0.5)
            html = self._get_html_from_url(issue_url)
        else:
            html = self._get_html_from_url(issue_url)

        if html:
            return self._get_articles_from_issue_page(html)
        return []

    @abstractmethod
    def _get_articles_from_issue_page(self, html: str):
        pass
    
    @abstractmethod
    def crawl(self, workers: int = 1, limit: int = None):
        pass

