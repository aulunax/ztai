import pymupdf4llm
import json

# markdown = pymupdf4llm.to_json("expected_results/article1.pdf")
# with open("article1.json", "w", encoding="utf-8") as f:
#     pretty_json = json.loads(markdown)
#     f.write(json.dumps(pretty_json, indent=4))



# from unstructured.partition.auto import partition
# blocks = partition(filename="expected_results/article1.pdf", languages=["pol", "eng", "lat"], strategy="auto")
# with open("article1_blocks.json", "w", encoding="utf-8") as f:
#     for block in blocks:
#         block_dict = {
#             "type": block.category,
#             "text": block.text
#         }
#         json.dump(block_dict, f, ensure_ascii=False)
#         f.write("\n")


# from unstructured.partition.auto import partition

# elements = partition("expected_results/article1.pdf")

# with open("article1_elements.json", "w", encoding="utf-8") as f:
#     f.write("\n\n".join([str(el) for el in elements]))

from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from marker.output import text_from_rendered
text, _, _ = text_from_rendered(
    PdfConverter(create_model_dict())("expected_results/article1.pdf")
)

with open("article1_marker.txt", "w", encoding="utf-8") as f:
    f.write(text)


# import textract
# text = textract.process("expected_results/article1.pdf").decode()
# with open("article1_textract.txt", "w", encoding="utf-8") as f:
#     f.write(text)