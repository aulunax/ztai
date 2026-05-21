# import json
# from PIL import Image
# from pathlib import Path

# from surya.input.processing import open_pdf, get_page_images, convert_if_not_rgb
# from surya.detection import DetectionPredictor

# pdf = "output/data/pressto/0969_16183/article.pdf"
# doc = open_pdf(pdf)
# page_count = len(doc)
# page_indices = list(range(page_count))
# images = get_page_images(doc, page_indices)
# doc.close()
# image_sizes = [img.size for img in images]
# correct_boxes = get_pdf_lines(pdf_path, image_sizes)

# det_predictor = DetectionPredictor()

# # predictions is a list of dicts, one per image
# predictions = det_predictor(pdf)

# with Path("testOcr.json").open("w", encoding="utf-8") as f:
#     json.dump(predictions, f, ensure_ascii=False, indent=2)

import json
from pathlib import Path

from paddleocr import PaddleOCR
from pdf2image import convert_from_path

# Initialize OCR
ocr = PaddleOCR(
    use_angle_cls=True,
    lang='en'
)

# Convert PDF pages to images
pages = convert_from_path("output/data/pressto/0969_16183/article.pdf", dpi=200)

# OCR each page
for i, page in enumerate(pages):
    image_path = f"page_{i}.png"
    page.save(image_path, "PNG")

    result = ocr.predict(image_path, cls=True)

    print(f"\n--- PAGE {i + 1} ---")

    # for line in result[0]:
    #     text = line[1][0]
    #     confidence = line[1][1]
    #     print(f"{confidence:.2f}  {text}")

    with Path("testOcr.json").open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
