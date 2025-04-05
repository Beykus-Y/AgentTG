# ./Agent/bot_lifecycle.py

import logging
import inspect
import json
import os  # <<< Добавили импорт os
import asyncio # <<< Добавили импорт asyncio
from typing import Dict, Any, Callable, Optional, List
from pathlib import Path  # <<< Добавили импорт Path
from aiogram import Dispatcher  # <<< Добавили импорт Dispatcher

# --- Основные зависимости ---
# Импортируем Bot и Dispatcher из загрузчика
try:
    from bot_loader import dp, bot
except ImportError:
    logging.critical("Failed to import dp, bot from bot_loader in bot_lifecycle.")
    raise

# Импортируем настройки (теперь только сам объект settings)
try:
    from config import settings
except ImportError:
    logging.critical("Failed to import settings from config in bot_lifecycle.")
    raise

# Импортируем модуль базы данных
try:
    import database
except ImportError:
    logging.critical("Failed to import database module in bot_lifecycle.")
    raise

# Импортируем модуль AI интерфейса
try:
    from ai_interface import gemini_api
except ImportError:
    logging.critical("Failed to import gemini_api from ai_interface in bot_lifecycle.")
    raise

# Импортируем доступные инструменты
try:
    from tools import available_functions as all_available_tools
except ImportError:
    logging.critical("Failed to import available_functions from tools in bot_lifecycle.")
    all_available_tools = {}

# --- Типы Google ---
try:
    # <<< ВОЗВРАЩАЕМ glm >>>
    from google.ai import generativelanguage as glm
    Content = glm.Content
    try:
        FinishReason = glm.Candidate.FinishReason
    except AttributeError: FinishReason = None
    # <<< GenerateContentResponse из types >>>
    from google.generativeai.types import GenerateContentResponse

    logger_types = logging.getLogger(__name__)
    logger_types.debug("Successfully imported Google types in bot_lifecycle")
except ImportError as e:
    logger_types = logging.getLogger(__name__)
    logger_types.warning(f"Could not import some Google types in bot_lifecycle: {e}")
    # <<< Обновляем заглушки >>>
    FinishReason, GenerateContentResponse, Content = Any, Any, Any

logger = logging.getLogger(__name__)

async def load_json_file(filepath: Optional[Path]) -> Optional[List[Dict]]: # <<< Принимаем Path
    """Вспомогательная функция для загрузки JSON."""
    if not filepath or not isinstance(filepath, Path) or not filepath.is_file(): # <<< Проверяем Path и is_file()
         logger.warning(f"JSON file path is invalid or file does not exist: {filepath}")
         return None
    try:
        # Открываем Path объект напрямую
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
             logger.error(f"JSON content in {filepath} is not a list.")
             return None
        logger.info(f"Loaded {len(data)} items from {filepath}.")
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed loading or parsing JSON file {filepath}: {e}", exc_info=True)
        return None

async def load_text_file(filepath: Optional[Path]) -> Optional[str]: # <<< Принимаем Path
    """Вспомогательная функция для загрузки текстового файла."""
    if not filepath or not isinstance(filepath, Path) or not filepath.is_file(): # <<< Проверяем Path и is_file()
         logger.warning(f"Text file path is invalid or file does not exist: {filepath}")
         return None
    try:
        # Открываем Path объект напрямую
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        logger.info(f"Loaded text file {filepath} ({len(content)} chars).")
        return content
    except OSError as e:
        logger.error(f"Failed loading text file {filepath}: {e}", exc_info=True)
        return None


async def on_startup(dispatcher: Dispatcher):
    """Инициализация ресурсов при старте бота."""
    logger.info("Executing bot startup sequence...")

    # 1. Инициализация БД
    try:
        await database.init_db()
        logger.info("Database schema initialized successfully.")
        # <<< Добавляем небольшую паузу для гарантии завершения дисковых операций >>>
        await asyncio.sleep(0.1)
        logger.debug("Short sleep after DB init finished.")
    except Exception as db_init_err:
        logger.critical(f"FATAL: Database initialization failed: {db_init_err}", exc_info=True)
        raise RuntimeError("Database initialization failed") from db_init_err

    # --- 1.5 Запуск NewsService ПОСЛЕ инициализации БД ---
    try:
        from services.news_service import news_service # Импортируем здесь
        if bot is None: # Проверка наличия бота
             raise RuntimeError("Bot instance is None, cannot start NewsService.")
        # Передаем экземпляр бота в сервис новостей.
        asyncio.create_task(news_service.start(bot))
        logger.info("News service background task scheduled after DB init.")
    except ImportError:
        logger.error("Could not import news_service. News feature unavailable.")
    except Exception as news_err:
        logger.error(f"Failed to start news service: {news_err}", exc_info=True)
    # -----------------------------------------------------

    # 2. Загрузка деклараций функций и промптов (ИСПОЛЬЗУЕМ ПУТИ ИЗ settings)
    lite_declarations = None
    pro_declarations = None
    lite_prompt = None
    pro_prompt = None

    try:
        # Загружаем декларации (если пути указаны и файлы существуют)
        if settings.lite_func_decl_file:
            lite_declarations = await load_json_file(settings.lite_func_decl_file) or [] # <<< Используем путь из settings
        if settings.pro_func_decl_file:
            pro_declarations = await load_json_file(settings.pro_func_decl_file) or [] # <<< Используем путь из settings

        # Загружаем промпты (если пути указаны и файлы существуют)
        if settings.lite_prompt_file:
            lite_prompt = await load_text_file(settings.lite_prompt_file) # <<< Используем путь из settings
        if settings.pro_prompt_file:
            pro_prompt = await load_text_file(settings.pro_prompt_file) # <<< Используем путь из settings

    except Exception as load_err:
        logger.error(f"Error loading prompts/declarations: {load_err}", exc_info=True)


    # 3. Инициализация моделей Gemini (ИСПОЛЬЗУЕМ ИМЕНА И НАСТРОЙКИ ИЗ settings)
    local_lite_model = None
    local_pro_model = None
    try:
        logger.info(f"Initializing Lite model: {settings.lite_gemini_model_name}")
        local_lite_model = gemini_api.setup_gemini_model(
            api_key=settings.google_api_key,
            function_declarations_data=lite_declarations, # Передаем загруженные (или None)
            model_name=settings.lite_gemini_model_name, # <<< Из settings
            system_prompt=lite_prompt,                  # Передаем загруженный (или None)
            generation_config=settings.lite_generation_config, # <<< Из settings
            safety_settings=settings.lite_safety_settings,    # <<< Из settings
            enable_function_calling=False # Lite v5 не использует FC
        )
        if not local_lite_model: raise ValueError("Lite model setup returned None")
        logger.info(f"Lite model '{settings.lite_gemini_model_name}' initialized.")

        logger.info(f"Initializing Pro model: {settings.pro_gemini_model_name}")
        local_pro_model = gemini_api.setup_gemini_model(
            api_key=settings.google_api_key,
            function_declarations_data=pro_declarations, # Передаем загруженные (или None)
            model_name=settings.pro_gemini_model_name, # <<< Из settings
            system_prompt=pro_prompt,                   # Передаем загруженный (или None)
            generation_config=settings.pro_generation_config, # <<< Из settings
            safety_settings=settings.pro_safety_settings,    # <<< Из settings
            enable_function_calling=settings.fc_enabled # <<< Из settings
        )
        if not local_pro_model: raise ValueError("Pro model setup returned None")
        logger.info(f"Pro model '{settings.pro_gemini_model_name}' initialized.")

    except Exception as model_init_err:
        logger.critical(f"FATAL: Gemini model initialization failed: {model_init_err}", exc_info=True)
        raise RuntimeError("Gemini model initialization failed") from model_init_err

    # 4. Маппинг хендлеров инструментов
    logger.info(f"Mapping {len(all_available_tools)} available tool handlers...")
    # Проверка соответствия декларациям (если они были загружены)
    if pro_declarations:
         declared_pro_func_names = {decl.get('name') for decl in pro_declarations if decl.get('name')}
         missing_handlers = declared_pro_func_names - set(all_available_tools.keys())
         if missing_handlers:
             logger.warning(f"Handlers not found for PRO functions declared in JSON: {missing_handlers}")
         extra_handlers = set(all_available_tools.keys()) - declared_pro_func_names
         if extra_handlers:
             logger.warning(f"Found tool handlers that are not declared in PRO JSON: {extra_handlers}")

    # 5. Сохраняем данные в dp.workflow_data
    dispatcher.workflow_data.update({
        "lite_model": local_lite_model,
        "pro_model": local_pro_model,
        "available_pro_functions": all_available_tools,
        # Используем параметры шагов FC из settings
        "max_lite_steps": settings.max_lite_fc_steps, # <<< Из settings
        "max_pro_steps": settings.max_pro_fc_steps  # <<< Из settings
    })
    logger.info("Models, tool handlers, and FC steps added to Dispatcher workflow_data.")

    # 6. Запуск фоновых задач (NewsService перенесен выше)
    # Здесь можно добавить запуск других сервисов, если они есть

    logger.info("Bot startup sequence complete!")


async def on_shutdown(dispatcher: Dispatcher):
    """Действия при остановке бота."""
    logger.info("Executing bot shutdown sequence...")

    # --- Останавливаем фоновые задачи ---
    try:
        from services.news_service import news_service # Импортируем здесь
        await news_service.stop()
        logger.info("News service stopped successfully.")
    except ImportError:
        logger.info("News service module not imported, skipping stop.")
    except Exception as e:
        logger.error(f"Error stopping News service during shutdown: {e}", exc_info=True)
    # (Добавить остановку других сервисов здесь)
    # ----------------------------------

    # Закрываем соединение с БД
    try:
        await database.close_db()
        logger.info("Database connection closed successfully.")
    except Exception as e:
        logger.error(f"Error closing database connection during shutdown: {e}", exc_info=True)

    # Очищаем workflow_data (опционально)
    dispatcher.workflow_data.clear()
    logger.info("Dispatcher workflow_data cleared.")

    # Закрываем сессию бота (aiogram обычно делает это сам при остановке)
    if bot and bot.session:
        try:
            await bot.session.close()
            logger.info("Bot session closed.")
        except Exception as e:
            logger.error(f"Error closing bot session: {e}", exc_info=True)

    logger.info("Bot shutdown sequence complete.")