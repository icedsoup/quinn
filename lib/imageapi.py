import cv2
import easyocr

_reader: easyocr.Reader | None = None

def _get_reader() -> easyocr.Reader:
    global _reader
    if _reader is None:
        _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _reader

def ocr_image(image_path: str) -> str:
    reader = _get_reader()
    results = reader.readtext(image_path, detail=0, paragraph=True)
    return " ".join(results).strip()

def ocr_print_number(image_path: str) -> str:
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return ""
    img = cv2.resize(img, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    _, thresh = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    reader = _get_reader()
    results = reader.readtext(thresh, detail=0, allowlist="0123456789·.")
    return "".join(results).strip()