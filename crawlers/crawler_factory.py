from .sejm_crawler import SejmCrawler
from .journals_crawler import JournalsCrawler
from .czasopisma_crawler import CzasopismaCrawler
from .pressto_crawler import PresstoCrawler

class CrawlerFactory:
    @staticmethod
    def create_crawler(crawler_type, sejm_issues_file=None):
        if crawler_type == "sejm":
            return SejmCrawler(issues_cache_path=sejm_issues_file)
        elif crawler_type == "journals":
            return JournalsCrawler()
        elif crawler_type == "czasopisma":
            return CzasopismaCrawler()
        elif crawler_type == "pressto":
            return PresstoCrawler()
        else:
            raise ValueError(f"Unknown crawler type: {crawler_type}")
