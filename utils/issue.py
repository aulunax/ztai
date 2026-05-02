
class ArticleData:
    def __init__(self, title=None, issue=None, journal=None, url=None, pdf_url=None, license=None, language=None):
        self.title = title
        self.issue = issue
        self.journal = journal
        self.url = url
        self.pdf_url = pdf_url
        self.license = license
        self.language = language

    def __str__(self):
        return f"ArticleData(title={self.title}, issue={self.issue}, journal={self.journal}, url={self.url}, pdf_url={self.pdf_url})"
    
    def to_json(self):
        return {
            "article_url": self.url,
            "journal_name": self.journal,
            "issue": self.issue,
            "title": self.title,
            "language": self.language,
            "license": self.license,
            "pdf_url": self.pdf_url,
        }