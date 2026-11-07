# brain/ocr_engine.py
import numpy as np
import easyocr

class OCREngine:
    def __init__(self):
        print("[Система] Загрузка EasyOCR (GPU)...")
        # Инициализируем один раз при запуске
        self.reader = easyocr.Reader(['en'], gpu=False)

    def extract_text(self, img):
        """Принимает PIL Image, возвращает распознанный текст"""
        img_np = np.array(img)
        bounds = self.reader.readtext(img_np, detail=0)
        return " ".join(bounds).strip()