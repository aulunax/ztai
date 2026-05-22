
from abc import ABC, abstractmethod
import logging
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

STOP_MARKERS = ["bibliografia", "summary"]

def is_stop_marker(text: str) -> bool:
    return text.strip().lower() in STOP_MARKERS


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

    def _extract_text_from_pdf_ocr(self, pdf_path: Path) -> str:
        from paddleocr import PaddleOCRVL
        try:
            pipeline = PaddleOCRVL(vl_rec_backend="vllm-server", vl_rec_server_url="http://localhost:8119/v1")

            output = pipeline.predict(input=str(pdf_path), lang="pl", text_det_thresh=0.3, text_det_box_thresh=0.6, text_det_unclip_ratio=2.0, text_rec_score_thresh=0.0)

            pages_res = list(output)

            output = pipeline.restructure_pages(pages_res, merge_tables=True, relevel_titles=True, concatenate_pages=True)

            pdf_full = output[0].json['res']

            blocks = self._extract_blocks(pdf_full)

            filtered_blocks = []
            for block in blocks:
                if block["label"] in ("text", "paragraph_title"):
                    block["text"] = self._strip_annotations(block["text"])
                    filtered_blocks.append(block)

            cut_index = None

            for i, block in enumerate(filtered_blocks):
                if is_stop_marker(block["text"]):
                    cut_index = i
                    break

            if cut_index is not None:
                filtered_blocks = filtered_blocks[:cut_index]

            # merge consecutive text blocks
            merged_text_blocks = []
            current_text_blocks = []
            for block in filtered_blocks:
                if block["label"] == "paragraph_title":
                    if current_text_blocks:
                        merged_text_blocks.append(" ".join(block["text"] for block in current_text_blocks))
                        current_text_blocks = []
                    merged_text_blocks.append(block["text"])
                else:
                    current_text_blocks.append(block)

            if current_text_blocks:
                merged_text_blocks.append(" ".join(block["text"] for block in current_text_blocks))


            # create a text file with just the text content of the blocks, separated by newlines
            text_output = "\n\n".join(text for text in merged_text_blocks)

            return text_output

            with Path("test_paddle_output_my.json").open("w", encoding="utf-8") as f:
                json.dump(pdf_full, f, ensure_ascii=False, indent=2)

            for res in output:
                print("a")
                res.save_to_json(save_path="test_paddle_output.json")  # get the structured result as a dict
                res.save_to_markdown(save_path="test_paddle_output.md")

            # save output to file
            with Path("test_paddle_output_my.json").open("w", encoding="utf-8") as f:
                json.dump(pdf_full, f, ensure_ascii=False, indent=2)

        except Exception as exc:
            self.logger.warning("PaddleOCR extraction failed for %s: %s", pdf_path, exc)
            return ""
