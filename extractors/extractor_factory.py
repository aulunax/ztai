from .pressto_extractor import PresstoExtractor
from .sejm_extractor import SejmExtractor
from .czasopisma_extractor import CzasopismaExtractor
from .journals_extractor import JournalsExtractor

class ExtractorFactory:
    @staticmethod
    def create_extractor(extractor_type):
        if extractor_type == "pressto":
            return PresstoExtractor()
        elif extractor_type == "sejm":
            return SejmExtractor()
        elif extractor_type == "czasopisma":
            return CzasopismaExtractor()
        elif extractor_type == "journals":
            return JournalsExtractor()
        else:
            raise ValueError("Unknown extractor type")
