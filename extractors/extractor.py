
from abc import ABC, abstractmethod
import logging

class Extractor(ABC):
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    def extract(self, data, limit: int = None):
        pass
