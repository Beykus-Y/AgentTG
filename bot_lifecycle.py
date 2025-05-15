# ./Agent/bot_lifecycle.py

import logging
import inspect
import json
import os
import asyncio
from typing import Dict, Any, Callable, Optional, List
from pathlib import Path
from aiogram import Dispatcher

# --- Условный импорт AI библиотек ---
try:
    import google.generativeai as genai
    # Импорт типов Google
    from google.ai import generativelanguage as glm
    Content = glm.Content
    try: FinishReason = glm.Candidate.FinishReason
    except AttributeError: FinishReason = None
    from google.generativeai.types import GenerateContentResponse as GoogleGenerateContentResponse
    google_types_imported = True
except ImportError:
    genai = None
    Content, FinishReason, GoogleGenerateContentResponse = Any, Any, Any
    google_types_imported = False
    logging.warning("Could not import Google Generative AI libraries.")

try:
    from openai import AsyncOpenAI
    from openai.types.chat import ChatCompletion as OpenAIChatCompletion
    openai_imported = True
except ImportError:
    AsyncOpenAI = None # type: ignore
    OpenAIChatCompletion = Any # type: ignore
    openai_imported = False
    logging.warning("Could not import OpenAI library.")

# --- Остальные импорты ---
try: from bot_loader import dp, bot
except ImportError: logging.critical("Failed to import dp, bot from bot_loader"); raise
try: from config import settings
except ImportError: logging.critical("Failed to import settings from config"); raise
try: import database
except ImportError: logging.critical("Failed to import database module"); raise
# --- Импортируем ОБА модуля API интерфейса ---
try: from ai_interface import gemini_api
except ImportError: gemini_api = None; logging.error("Failed to import gemini_api.")
try: from ai_interface import openai_api # Наш новый модуль
except ImportError: openai_api = None; logging.error("Failed to import openai_api.")
# --- Импорт инструментов ---
try:
    from tools import available_functions as all_available_tools
    logger_tools = logging.getLogger(__name__ + ".tools")
    logger_tools.info(f"Successfully imported available_functions from tools.")
    logger_tools.info(f"Number of functions found by tools init: {len(all_available_tools)}")
except ImportError as e:
     logging.critical(f"CRITICAL IMPORT ERROR: Failed to import from tools: {e}", exc_info=True)
     all_available_tools = {}
except Exception as e:
     logging.critical(f"CRITICAL UNEXPECTED ERROR during tools import: {e}", exc_info=True)
     all_available_tools = {}

logger = logging.getLogger(__name__)

# --- Вспомогательные функции загрузки файлов (без изменений) ---
async def load_json_file(filepath: Optional[Path]) -> Optional[List[Dict]]:
    if not filepath or not isinstance(filepath, Path) or not filepath.is_file():
         logger.warning(f"JSON file path is invalid or file does not exist: {filepath}")
         return None
    try:
        with open(filepath, "r", encoding="utf-8") as f: data = json.load(f)
        if not isinstance(data, list):
             logger.error(f"JSON content in {filepath} is not a list."); return None
        logger.info(f"Loaded {len(data)} items from {filepath}.")
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed loading or parsing JSON file {filepath}: {e}", exc_info=True); return None

async def load_text_file(filepath: Optional[Path]) -> Optional[str]:
    if not filepath or not isinstance(filepath, Path) or not filepath.is_file():
         logger.warning(f"Text file path is invalid or file does not exist: {filepath}")
         return None
    try:
        with open(filepath, "r", encoding="utf-8") as f: content = f.read()
        logger.info(f"Loaded text file {filepath} ({len(content)} chars).")
        return content
    except OSError as e:
        logger.error(f"Failed loading text file {filepath}: {e}", exc_info=True); return None

# --- Функции управления индексом ключей (только для Google) ---
def get_current_api_key_index(dp: Dispatcher) -> int:
    return dp.workflow_data.get("current_api_key_index", 0)

def increment_api_key_index(dp: Dispatcher) -> int:
    keys = dp.workflow_data.get("google_api_keys", [])
    if not keys: return 0
    current_index = dp.workflow_data.get("current_api_key_index", 0)
    next_index = (current_index + 1) % len(keys)
    dp.workflow_data["current_api_key_index"] = next_index
    logger.info(f"Google API Key index incremented. New index: {next_index} (Key: ...{keys[next_index][-4:]})")
    return next_index

# --- <<< НОВАЯ ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ КОНВЕРТАЦИИ ТИПОВ >>> ---
def convert_gemini_params_to_openai_schema(params_schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Рекурсивно конвертирует схему параметров из формата Gemini (с типами вроде "STRING")
    в формат JSON Schema, ожидаемый OpenAI (с типами вроде "string").
    """
    if not isinstance(params_schema, dict):
        return params_schema

    converted_schema = params_schema.copy() # Копируем, чтобы не изменять оригинал

    if "type" in converted_schema and isinstance(converted_schema["type"], str):
        # Основное преобразование типов
        type_mapping = {
            "STRING": "string",
            "INTEGER": "integer",
            "NUMBER": "number", # Gemini может использовать NUMBER, OpenAI тоже
            "BOOLEAN": "boolean",
            "OBJECT": "object",
            "ARRAY": "array"
            # Добавьте другие маппинги, если необходимо
        }
        original_type = converted_schema["type"]
        converted_schema["type"] = type_mapping.get(original_type.upper(), original_type) # Преобразуем, если есть в маппинге

    if "properties" in converted_schema and isinstance(converted_schema["properties"], dict):
        converted_properties = {}
        for prop_name, prop_schema in converted_schema["properties"].items():
            converted_properties[prop_name] = convert_gemini_params_to_openai_schema(prop_schema)
        converted_schema["properties"] = converted_properties

    if "items" in converted_schema and isinstance(converted_schema["items"], dict): # Для типа "array"
        converted_schema["items"] = convert_gemini_params_to_openai_schema(converted_schema["items"])

    return converted_schema
# --- <<< КОНЕЦ НОВОЙ ФУНКЦИИ >>> ---

# --- ИЗМЕНЕННЫЙ on_startup ---
async def on_startup(dispatcher: Dispatcher, ai_provider: str):
    """Инициализация ресурсов при старте бота с учетом выбранного AI провайдера."""
    logger.info(f"Executing bot startup sequence for AI provider: {ai_provider.upper()}")

    # 0. Проверка обязательных зависимостей
    if database is None: raise RuntimeError("Database module failed to load.")
    if not all_available_tools: logger.warning("Tools module failed or no tools found.")

    # 1. Инициализация БД
    try:
        await database.init_db()
        logger.info("Database schema initialized successfully.")
        await asyncio.sleep(0.1)
    except Exception as db_init_err:
        logger.critical(f"FATAL: DB initialization failed: {db_init_err}", exc_info=True); raise

    # --- 1.5 Запуск NewsService ---
    try:
        from services.news_service import news_service
        if bot is None: raise RuntimeError("Bot instance is None...")
        asyncio.create_task(news_service.start(bot))
        logger.info("News service background task scheduled.")
    except ImportError: logger.error("Could not import news_service...")
    except Exception as news_err: logger.error(f"Failed start news service: {news_err}", exc_info=True)

    # 2. Загрузка общих промптов и деклараций
    pro_declarations = await load_json_file(settings.pro_func_decl_file) if settings.pro_func_decl_file else None
    lite_prompt = await load_text_file(settings.lite_prompt_file) if settings.lite_prompt_file else None
    pro_prompt = await load_text_file(settings.pro_prompt_file) if settings.pro_prompt_file else None

    # 3. Инициализация AI в зависимости от выбора
    workflow_update_data = {"ai_provider": ai_provider}

    if ai_provider == "google":
        logger.info("Initializing Google Generative AI...")
        if not genai or not gemini_api or not google_types_imported: raise RuntimeError("Google AI libs/types missing.")
        if not settings.google_api_keys: raise RuntimeError("Missing Google API keys.")

        lite_models_list = []
        pro_models_list = []
        for index, api_key in enumerate(settings.google_api_keys):
             logger.info(f"Initializing Google models key index {index} (...{api_key[-4:]})")
             try:
                 genai.configure(api_key=api_key)
                 # Lite Model
                 current_lite_model = gemini_api.setup_gemini_model(
                     api_key=api_key, model_name=settings.lite_gemini_model_name,
                     system_prompt=lite_prompt, generation_config=settings.lite_generation_config,
                     safety_settings=settings.lite_safety_settings, enable_function_calling=False
                 )
                 if not current_lite_model: raise ValueError(f"Lite setup failed index {index}")
                 lite_models_list.append(current_lite_model)
                 logger.info(f"Google Lite '{settings.lite_gemini_model_name}' init index {index}.")
                 # Pro Model
                 current_pro_model = gemini_api.setup_gemini_model(
                     api_key=api_key, model_name=settings.pro_gemini_model_name,
                     system_prompt=pro_prompt, function_declarations_data=pro_declarations,
                     generation_config=settings.pro_generation_config, safety_settings=settings.pro_safety_settings,
                     enable_function_calling=settings.fc_enabled
                 )
                 if not current_pro_model: raise ValueError(f"Pro setup failed index {index}")
                 pro_models_list.append(current_pro_model)
                 logger.info(f"Google Pro '{settings.pro_gemini_model_name}' init index {index}.")
             except Exception as model_init_err:
                 logger.error(f"Google model init failed index {index}: {model_init_err}", exc_info=True)
                 logger.warning(f"Skipping Google models for key index {index}.")

        if not lite_models_list or not pro_models_list: raise RuntimeError("No Google models initialized.")

        workflow_update_data.update({
            "lite_models_list": lite_models_list,
            "pro_models_list": pro_models_list,
            "google_api_keys": settings.google_api_keys,
            "current_api_key_index": 0,
            "pro_declarations": pro_declarations # Сохраняем для информации/проверки
        })
        logger.info(f"Initialized {len(pro_models_list)} Google Pro and {len(lite_models_list)} Lite models.")

    elif ai_provider == "openai":
        logger.info("Initializing OpenAI...")
        if AsyncOpenAI is None or openai_api is None or not openai_imported: raise RuntimeError("OpenAI lib/module missing.")
        if not settings.openai_api_key: raise RuntimeError("Missing OpenAI API key.")

        try:
            openai_client = AsyncOpenAI(
                api_key=settings.openai_api_key,
                organization=settings.openai_organization_id,
                # --- <<< ДОБАВЛЕНО: Передача base_url, если он задан >>> ---
                base_url=settings.openai_base_url if hasattr(settings, 'openai_base_url') and settings.openai_base_url else None
            )
            # --- <<< ИЗМЕНЕНО: Преобразование деклараций в формат OpenAI tools >>> ---
            openai_tools = None
            if settings.fc_enabled and pro_declarations:
                openai_tools = []
                for decl in pro_declarations:
                    if isinstance(decl, dict) and decl.get("name") and decl.get("description"):
                         # <<< ИСПОЛЬЗУЕМ КОНВЕРТЕР ДЛЯ ПАРАМЕТРОВ >>>
                         converted_params = convert_gemini_params_to_openai_schema(decl.get("parameters", {}))
                         openai_tool_entry = {
                             "type": "function",
                             "function": {
                                 "name": decl["name"],
                                 "description": decl["description"],
                                 "parameters": converted_params # Используем сконвертированные параметры
                             }
                         }
                         openai_tools.append(openai_tool_entry)
                    else:
                         logger.warning(f"Skipping invalid declaration during OpenAI tool conversion: {decl}")
                if openai_tools:
                    logger.info(f"Converted and prepared {len(openai_tools)} tools for OpenAI API calls.")
                else:
                     logger.warning("No valid declarations found to convert for OpenAI tools.")
                     openai_tools = None
            # --- <<< КОНЕЦ ИЗМЕНЕНИЯ >>> ---

            workflow_update_data.update({
                "openai_client": openai_client,
                "lite_openai_model": settings.lite_openai_model_name,
                "pro_openai_model": settings.pro_openai_model_name,
                "lite_system_prompt": lite_prompt,
                "pro_system_prompt": pro_prompt,
                "openai_tools": openai_tools, # <<< Сохраняем преобразованный список
                "openai_temperature": settings.openai_temperature,
                "openai_max_tokens": settings.openai_max_tokens,
            })
            logger.info(f"OpenAI AsyncClient initialized (Base URL: {openai_client.base_url}).")

        except Exception as openai_init_err:
            logger.critical(f"FATAL: OpenAI client init failed: {openai_init_err}", exc_info=True); raise

    else: # Неизвестный провайдер
        logger.critical(f"FATAL: Unknown AI provider: {ai_provider}"); raise ValueError(f"Invalid AI provider")

    # 4. Общие данные для workflow_data
    logger.info(f"Mapping {len(all_available_tools)} available tool handlers...")
    if pro_declarations and settings.fc_enabled:
         declared_func_names = {decl.get('name') for decl in pro_declarations if decl.get('name')}
         missing_handlers = declared_func_names - set(all_available_tools.keys())
         if missing_handlers: logger.warning(f"Handlers missing for declared functions: {missing_handlers}")
         extra_handlers = set(all_available_tools.keys()) - declared_func_names
         if extra_handlers: logger.warning(f"Found handlers not declared in JSON: {extra_handlers}")

    workflow_update_data.update({
        "available_pro_functions": all_available_tools,
        "max_pro_steps": settings.max_pro_fc_steps
    })

    # 5. Сохраняем все данные в dp.workflow_data
    dispatcher.workflow_data.update(workflow_update_data)
    logger.info(f"AI specific data for '{ai_provider.upper()}' and common data added to Dispatcher workflow_data.")
    logger.debug(f"Current workflow_data keys: {list(dispatcher.workflow_data.keys())}")

    # 6. Запуск фоновых задач
    # ...

    logger.info("Bot startup sequence complete!")


# --- on_shutdown (без изменений) ---
async def on_shutdown(dispatcher: Dispatcher):
    """Действия при остановке бота."""
    logger.info("Executing bot shutdown sequence...")
    # --- Остановка фоновых задач ---
    try:
        from services.news_service import news_service
        await news_service.stop()
        logger.info("News service stopped successfully.")
    except ImportError: logger.info("News service not imported, skipping stop.")
    except Exception as e: logger.error(f"Error stopping News service: {e}", exc_info=True)
    # --- Закрытие БД ---
    try: await database.close_db(); logger.info("Database connection closed.")
    except Exception as e: logger.error(f"Error closing database: {e}", exc_info=True)
    # --- Очистка workflow_data ---
    dispatcher.workflow_data.clear(); logger.info("Dispatcher workflow_data cleared.")
    # --- Закрытие сессии бота ---
    if bot and bot.session:
        try: await bot.session.close(); logger.info("Bot session closed.")
        except Exception as e: logger.error(f"Error closing bot session: {e}", exc_info=True)
    logger.info("Bot shutdown sequence complete.")