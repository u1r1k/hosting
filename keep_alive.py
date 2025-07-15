import logging
import threading
import time

# 🔧 Настройка логирования
logging.basicConfig(
    format='[%(asctime)s] %(levelname)s - %(message)s',
    level=logging.INFO
)

# 🔁 Keep-alive функция
def keep_alive():
    while True:
        logging.info("✅ Бот в порядке (ping)")
        time.sleep(60)  # Каждые 60 секунд

# ▶️ Запуск в отдельном потоке
threading.Thread(target=keep_alive, daemon=True).start()
