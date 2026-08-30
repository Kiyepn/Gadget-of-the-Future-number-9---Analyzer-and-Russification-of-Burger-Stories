import time
import requests
from PIL import ImageGrab, ImageStat
import easyocr

# У ИИ-распознавателя нет никаких путей к exe-файлам! 
# Просто инициализируем английский ('en') язык
print("[Система] Загрузка нейросети распознавания текста...")
reader = easyocr.Reader(['en'], gpu=True) # gpu=True задействует твою RTX 3050

print("[Лаборатория Гаджетов Будущего] База на Windows успешно запущена!")
print("Нажимай Win+Shift+S, выделяй английский текст в новелле...")
last_stat = None

while True:
    time.sleep(0.5)
    img = get_last_image() if 'get_last_image' in globals() else ImageGrab.grabclipboard()
    
    if img and not isinstance(img, list):
        try:
            stat = sum(ImageStat.Stat(img).sum)
        except Exception:
            continue
            
        if stat != last_stat:
            last_stat = stat
            print("\n[Система] Обнаружен скриншот! Нейросеть читает экран...")
            
            try:
                # Читаем текст с картинки через EasyOCR
                                # Конвертируем скриншот в байты, чтобы ИИ-распознаватель её скушал
                import io
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='PNG')
                img_bytes = img_byte_arr.getvalue()
                
                # Теперь передаем чистые байты в EasyOCR
                bounds = reader.readtext(img_bytes, detail=0)

                text = " ".join(bounds).strip()
                
                if text:
                    print(f"[Оригинал]: {text}")
                    
                    # Отправка текста в Ollama
                    try:
                        response = requests.post('http://localhost:11434/api/generate', json={
                            "model": "qwen2.5:7b", 
                            "prompt": f"Ты профессиональный переводчик визуальных новелл. Переведи этот английский текст на русский язык живым, атмосферным и литературным языком. Выдай ТОЛЬКО перевод: {text}",
                            "stream": False
                        })
                        result = response.json()
                        print(f"[Перевод от RTX 3050]: {result['response']}")
                    except Exception:
                        print("[Ошибка]: Не удалось связаться с Ollama! Проверь, что сервис активен.")
                else:
                    print("[Система] Текст на картинке не найден.")
            except Exception as e:
                print(f"[Ошибка распознавания]: {e}")
