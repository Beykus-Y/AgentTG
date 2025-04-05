# bot_loader.py

import logging
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage # Используем MemoryStorage по умолчанию

# Импортируем настройки из корневого config.py
try:
    from config import settings
except ImportError:
    # Заглушка на случай проблем с импортом config
    # В реальном приложении лучше убедиться, что config.py доступен
    class MockSettings:
        bot_token: str = "YOUR_BOT_TOKEN_HERE" # Замените реальным токеном или обработайте ошибку
    settings = MockSettings()
    logging.critical(
        "CRITICAL: Could not import 'settings' from config.py in bot_loader. "
        "Using mock settings. Please check your project structure and config.py."
    )
    # В продакшене здесь лучше выбрасывать исключение или выходить
    # import sys
    # sys.exit("Configuration error: Cannot load settings.")

logger = logging.getLogger(__name__)

# --- Инициализация Хранилища FSM ---
# Используем MemoryStorage, если не требуется более сложное хранилище (например, Redis).
# Для Redis:
# from aiogram.fsm.storage.redis import RedisStorage
# storage = RedisStorage.from_url('redis://localhost:6379/0')
storage = MemoryStorage()
logger.info("FSM storage initialized (MemoryStorage).")

# --- Инициализация Бота ---
# Используем parse_mode=MarkdownV2, так как утилита escape_markdown_v2 работает с ним.
# Убедитесь, что settings.bot_token действительно содержит ваш токен.
try:
    if not settings.bot_token or settings.bot_token == "YOUR_BOT_TOKEN_HERE":
         raise ValueError("Bot token is missing or is a placeholder in config/settings.")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.MARKDOWN_V2
        )
    )
    logger.info("Bot instance created successfully.")
except ValueError as ve:
     logger.critical(f"Configuration error: {ve}")
     exit(1) # Выход, если токен не задан
except Exception as e:
    logger.critical(f"Failed to create Bot instance: {e}", exc_info=True)
    exit(1) # Выход из приложения, так как без бота оно не сможет работать

# --- Инициализация Диспетчера ---
# Передаем хранилище в диспетчер.
# workflow_data будет заполняться позже, в bot_lifecycle.on_startup.
try:
    dp = Dispatcher(storage=storage)
    logger.info("Dispatcher instance created successfully.")
except Exception as e:
    logger.critical(f"Failed to create Dispatcher instance: {e}", exc_info=True)
    exit(1)

# Экземпляры bot и dp теперь можно импортировать из этого модуля
# в другие части приложения (например, в bot_lifecycle, handlers),
# чтобы избежать циклических импортов.