from .pressto_extractor import PresstoExtractor
from .sejm_extractor import SejmExtractor
from .czasopisma_extractor import CzasopismaExtractor
from .journals_extractor import JournalsExtractor

class ExtractorFactory:
    @staticmethod
    def create_extractor(extractor_type, output_dir=None, skip_download=False):
        if extractor_type == "pressto":
            return PresstoExtractor(output_dir=output_dir, skip_download=skip_download)
        elif extractor_type == "sejm":
            return SejmExtractor()
        elif extractor_type == "czasopisma":
            return CzasopismaExtractor(output_dir=output_dir, skip_download=skip_download)
        elif extractor_type == "journals":
            return JournalsExtractor(output_dir=output_dir, skip_download=skip_download)
        else:
            raise ValueError("Unknown extractor type")
