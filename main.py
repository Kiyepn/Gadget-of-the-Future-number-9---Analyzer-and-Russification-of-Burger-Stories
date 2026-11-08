# main.py
import time
import os
import ctypes # Для очистки буфера обмена
from PIL import ImageGrab
import keyboard

from brain.ocr_engine import OCREngine
from brain.llm_engine import translate, analyze_character
from brain.memory_engine import MEMORY_FILE
from brain.paths import ensure_data_dir

def main():
    print("==================================================")
    print("[Лаборатория Гаджетов Будущего] База v1.4.0")
    print("Кнопки управления:")
    print("Win+Shift+S - сделать скриншот и перевести")
    print("F8 - Пауза / Продолжить")
    print("F9 - Очистить память диалога")
    print("==================================================")
    
    # Гарантируем, что папка data/ существует рядом с приложением (или exe)
    ensure_data_dir()

    # Инициализация OCR движка
    ocr = OCREngine()
    
    last_hash = None
    is_paused = False

    while True:
        time.sleep(0.5)
        
        # --- Обработка горячих клавиш ---
        if keyboard.is_pressed('F8'):
            is_paused = not is_paused
            print(f"\n[Система] Пауза: {'ВКЛ' if is_paused else 'ВЫКЛ'}")
            time.sleep(0.5)
            
        if keyboard.is_pressed('F9'):
            try:
                with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
                    f.write("[]")
                print("\n[Система] Память диалога очищена!")
            except Exception as e:
                print(f"\n[Ошибка] Не удалось очистить память: {e}")
            time.sleep(1)
            
        if is_paused:
            continue

        # --- Захват буфера обмена ---
        img = ImageGrab.grabclipboard()
        
        if img and not isinstance(img, list):
            try:
                current_hash = hash(img.tobytes())
                
                if current_hash != last_hash:
                    last_hash = current_hash
                    print("\n[Система] Скриншот пойман!")
                    
                    # 1. Распознаем текст
                    text = ocr.extract_text(img)
                    
                    if text:
                        print(f"[OCR]: {text}")
                        print("[Нейросеть] Думает над переводом...")
                        
                        # 2. Переводим
                        translated_text = translate(text)
                        print(f"[Гаджет Будущего]: {translated_text}")
                        
                        # 3. Фоновый анализ
                        analyze_character(text)
                    else:
                        print("[Система] Чистый кадр, текста не обнаружено.")
                        
            except Exception as e:
                print(f"[Критическая ошибка]: {e}")
            
            # --- ОЧИЩАЕМ БУФЕР, ЧТОБЫ НЕ БЫЛО СПАМА ---
            try:
                ctypes.windll.user32.OpenClipboard(None)
                ctypes.windll.user32.EmptyClipboard()
                ctypes.windll.user32.CloseClipboard()
            except Exception:
                pass

if __name__ == "__main__":
    main()