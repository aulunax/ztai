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

from paddleocr import PaddleOCRVL

# Convert PDF pages to images
input_file = "./table.pdf"
output_dir = Path("./output_paddle")
output_dir.mkdir(parents=True, exist_ok=True)

pipeline = PaddleOCRVL(vl_rec_backend="vllm-server", vl_rec_server_url="http://localhost:8118/v1")

output = pipeline.predict(input=input_file)

pages_res = list(output)

output = pipeline.restructure_pages(pages_res)
# output = pipeline.restructure_pages(pages_res, merge_tables=True) # Merge tables across pages
# output = pipeline.restructure_pages(pages_res, merge_tables=True, relevel_titles=True) # Merge tables across pages and reconstruct multi-level titles
# output = pipeline.restructure_pages(pages_res, merge_tables=True, relevel_titles=True, concatenate_pages=True) # Merge tables across pages, reconstruct multi-level titles, and merge multiple pages

for res in output:
    res.print() ## Print the structured prediction output
    res.save_to_json(save_path=output_dir) ## Save the current image's structured result in JSON format
    res.save_to_markdown(save_path=output_dir) ## Save the current image's result in Markdown format


