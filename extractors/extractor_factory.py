from .pressto_extractor import PresstoExtractor
from .sejm_extractor import SejmExtractor
from .czasopisma_extractor import CzasopismaExtractor
from .journals_extractor import JournalsExtractor

class ExtractorFactory:
    @staticmethod
    def create_extractor(extractor_type, output_dir=None, skip_czasopisma_prep=False):
        if extractor_type == "pressto":
            return PresstoExtractor()
        elif extractor_type == "sejm":
            return SejmExtractor()
        elif extractor_type == "czasopisma":
            return CzasopismaExtractor(output_dir=output_dir, skip_preparation=skip_czasopisma_prep)
        elif extractor_type == "journals":
            return JournalsExtractor()
        else:
            raise ValueError("Unknown extractor type")
