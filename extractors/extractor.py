
from abc import ABC, abstractmethod
import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

class Extractor(ABC):
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = self._build_session()

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

    def _download_pdf(self, pdf_url: str, pdf_path) -> bool:
        try:
            response = self.session.get(pdf_url, timeout=30)
            response.raise_for_status()
            pdf_path.write_bytes(response.content)
            return True
        except requests.RequestException as exc:
            self.logger.warning("Failed to download PDF %s: %s", pdf_url, exc)
            return False

    @staticmethod
    def _format_eta(seconds: float) -> str:
        total_seconds = max(0, int(round(seconds)))
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    @abstractmethod
    def extract(self, data, limit: int = None, start_at_index: int = 0):
        pass
