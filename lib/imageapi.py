from time import perf_counter

import cv2
import easyocr

from telemetry import emit

_reader: easyocr.Reader | None = None

def _get_reader(trace=None) -> easyocr.Reader:
    global _reader
    if _reader is None:
        started = perf_counter()
        emit('sniper', 'ocr', 'info', 'Loading English recognition model', trace=trace, engine='EasyOCR', device='CPU')
        _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        emit('sniper', 'ocr', 'info', 'Recognition model loaded', trace=trace, model_load_ms=round((perf_counter() - started) * 1000, 1), engine='EasyOCR', device='CPU')
    return _reader

def ocr_image(image_path: str, trace=None, card=None, field='text') -> str:
    reader = _get_reader(trace)
    started = perf_counter()
    results = reader.readtext(image_path, detail=0, paragraph=True)
    text = " ".join(results).strip()
    emit('sniper', 'ocr', 'info', f'Card {card} · {field}: {text or "No text detected"}', trace=trace,
         card=card, field=field, text=text, pass_ms=round((perf_counter() - started) * 1000, 1), regions=len(results))
    return text

def ocr_print_number(image_path: str, trace=None, card=None) -> str:
    started = perf_counter()
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        emit('sniper', 'ocr', 'info', 'Print crop could not be read', trace=trace, card=card, field='print')
        return ""
    img = cv2.resize(img, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    _, thresh = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    reader = _get_reader(trace)
    results = reader.readtext(thresh, detail=0, allowlist="0123456789·.")
    text = "".join(results).strip()
    emit('sniper', 'ocr', 'info', f'Card {card} · print: {text or "No digits detected"}', trace=trace,
         card=card, field='print', text=text, pass_ms=round((perf_counter() - started) * 1000, 1), regions=len(results))
    return text
