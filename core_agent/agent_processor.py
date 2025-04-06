# core_agent/agent_processor.py

import logging
import json
import re # Импорт для регулярных выражений (проверка упоминаний)
from typing import Optional, Dict, Any, List, Callable

# --- Aiogram и зависимости ---
from aiogram import types, Bot, Dispatcher # <<< Добавлен Dispatcher
from aiogram.enums import ChatType as aiogram_ChatType
# --- Локальные импорты ядра ---
from .history_manager import prepare_history, save_history
from .ai_interaction import process_request
from .result_parser import extract_text
# --- Импорт парсера ответа Lite-модели ---
from .response_parsers import parse_lite_llm_response # Используем новое имя файла
# --- Импорт утилит и глобальных объектов ---
from utils.helpers import escape_markdown_v2
from bot_loader import dp, bot as bot_instance # Импортируем dp и бот
# --- Импорт функций управления индексом (если используется для Lite) ---
# Если Lite не требует ротации ключей, этот импорт не нужен здесь
from bot_lifecycle import get_current_api_key_index # <<< Импортируем для выбора Lite модели

# --- Импорт БД и CRUD операций ---
import database
from database.crud_ops.profiles import upsert_user_profile

# (Опционально, если работаете с объектами Content напрямую)
# from google.ai import generativelanguage as glm
# Content = glm.Content

logger_ap = logging.getLogger(__name__)
logger_ap.info("--- Loading agent_processor.py ---")

# --- Кэш информации о боте ---
BOT_INFO_CACHE: Dict[str, Any] = {"info": None, "username_lower": None}

async def _get_bot_info(bot_to_use: Bot) -> Optional[types.User]:
    """Получает и кэширует информацию о боте."""
    global BOT_INFO_CACHE
    if BOT_INFO_CACHE["info"] is None and bot_to_use:
        try:
            bot_user = await bot_to_use.get_me()
            BOT_INFO_CACHE["info"] = bot_user
            BOT_INFO_CACHE["username_lower"] = bot_user.username.lower() if bot_user.username else None
            logger_ap.info(f"Bot info cached in agent_processor: ID={bot_user.id}, Username=@{bot_user.username}")
        except Exception as e:
            logger_ap.error(f"Failed to get bot info via API in agent_processor: {e}")
            BOT_INFO_CACHE["info"] = None
            BOT_INFO_CACHE["username_lower"] = None
    return BOT_INFO_CACHE["info"]

# --- Вспомогательная функция для вызова Pro-модели ---
async def _execute_pro_model_logic(
    message: types.Message,
    # <<< ИЗМЕНЕНИЕ: Принимаем СПИСКИ моделей >>>
    # pro_model: Any, # Удалено
    pro_models_list: List[Any],
    lite_models_list: List[Any], # Опционально, если нужно передавать дальше
    available_pro_functions: Dict[str, Callable],
    max_pro_steps: int,
    # <<< ДОБАВЛЕНО: dispatcher >>>
    dispatcher: Dispatcher
) -> Optional[str]:
    """
    Выполняет стандартную логику обработки Pro моделью, используя списки моделей
    и передавая dispatcher для управления ключами API.
    """
    chat_id=message.chat.id
    user_id=message.from_user.id if message.from_user else 0
    chat_type=message.chat.type
    user_input=message.text or ""
    add_user_context = True # По умолчанию добавляем контекст

    logger_ap.debug(f"Executing Pro model logic for chat {chat_id} using multi-key setup.")

    try:
        # --- Подготовка истории (остается без изменений) ---
        initial_history_obj_list, original_db_len = await prepare_history(
            chat_id=chat_id,
            user_id=user_id,
            chat_type=chat_type,
            add_notes=add_user_context
        )
        # Убедимся, что initial_history_obj_list - это список Content объектов
        if not isinstance(initial_history_obj_list, list):
             logger_ap.error(f"prepare_history did not return a list for chat {chat_id}. Type: {type(initial_history_obj_list)}")
             return escape_markdown_v2("Ошибка подготовки истории диалога.")

        # --- Взаимодействие с Pro AI ---
        # <<< ИЗМЕНЕНИЕ: Передаем dispatcher вместо model_instance >>>
        final_history_obj_list, interaction_error_msg, last_func_name, last_sent_text, last_func_result = await process_request(
            # model_instance=... # Удалено
            initial_history=initial_history_obj_list, # Передаем список Content объектов
            user_input=user_input,
            available_functions=available_pro_functions,
            max_steps=max_pro_steps,
            chat_id=chat_id,
            user_id=user_id,
            chat_type=chat_type,
            dispatcher=dispatcher # Передаем dispatcher
        )

        # --- Обработка результата Pro AI (остается без изменений) ---
        error_message_for_user: Optional[str] = None
        final_response_text_escaped: Optional[str] = None

        if interaction_error_msg:
            logger_ap.error(f"Core Agent (Pro): AI interaction failed for chat {chat_id}: {interaction_error_msg}")
            # Экранируем сообщение об ошибке от process_request
            error_message_for_user = f"Произошла ошибка при обработке: {escape_markdown_v2(interaction_error_msg)}"
        elif final_history_obj_list:
            final_response_text_raw = extract_text(final_history_obj_list)

            if last_func_name == 'send_telegram_message' and final_response_text_raw:
                logger_ap.info(f"Suppressing final text output because last successful action was send_telegram_message. Chat: {chat_id}")
                final_response_text_raw = None

            if final_response_text_raw:
                logger_ap.info(f"Core Agent (Pro): Final text (len={len(final_response_text_raw)}) will be sent for chat {chat_id}.")
                final_response_text_escaped = escape_markdown_v2(final_response_text_raw)
            else:
                reason = "Model generated no text" if last_func_name != 'send_telegram_message' else "Text suppressed after send_telegram_message"
                log_level = logging.INFO if last_func_name or last_sent_text else logging.WARNING # Учитываем last_sent_text
                logger_ap.log(log_level, f"Core Agent (Pro): No final text to send for chat {chat_id}. Reason: {reason}. (Last func: {last_func_name})")

            # --- Сохранение истории Pro (остается без изменений) ---
            if save_history:
                await save_history(
                      chat_id=chat_id,
                      final_history_obj_list=final_history_obj_list,
                      original_db_history_len=original_db_len,
                      current_user_id=user_id,
                      last_sent_message_text=last_sent_text # <<< ДОБАВЛЕНО ОБРАТНО
                )
            else:
                 logger_ap.error(f"Cannot save Pro history for chat {chat_id}: save_history function is not available.")
        else:
            logger_ap.error(f"Core Agent (Pro): AI interaction returned None history without error msg for chat {chat_id}")
            error_message_for_user = escape_markdown_v2("Модель AI не вернула корректный результат.")

        # --- Возврат результата Pro ---
        if error_message_for_user:
            # Сообщение об ошибке уже должно быть экранировано
            return error_message_for_user
        elif final_response_text_escaped:
            return final_response_text_escaped
        else:
            return None # Ни ошибки, ни текста

    except Exception as pro_err:
        logger_ap.error(f"Unexpected error during Pro model execution logic: {pro_err}", exc_info=True)
        return escape_markdown_v2("Произошла внутренняя ошибка при обработке вашего запроса.")


# --- Основная функция обработчика ---
async def handle_user_request(
    message: types.Message,
    force_pro_model: bool = False
) -> Optional[str]:
    """
    Основная точка входа для обработки входящего запроса пользователя.
    Управляет выбором модели (Lite/Pro) и делегирует выполнение.
    """
    chat_id = message.chat.id
    user = message.from_user
    user_id = user.id if user else 0
    chat_type = message.chat.type
    user_input = message.text or ""

    if user_id == 0:
         logger_ap.warning(f"Handling request in chat {chat_id} without user_id. Input ignored.")
         return None

    logger_ap.info(f"Core Agent: Handling request from user={user_id} in chat={chat_id} (type={chat_type}, force_pro={force_pro_model})")

    # --- Сохранение сообщения и профиля пользователя СРАЗУ (остается без изменений) ---
    if user and user_input:
        # ... (код сохранения в БД остается) ...
        try:
            await upsert_user_profile(
                user_id=user_id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name
            )
            user_parts_list = [{'text': user_input}]
            try:
                from utils.converters import _serialize_parts
                user_parts_json = _serialize_parts(user_parts_list)
                if database and hasattr(database, 'add_message_to_history'):
                     await database.add_message_to_history(
                         chat_id=chat_id,
                         role='user',
                         parts=user_parts_json,
                         user_id=user_id
                     )
                else:
                    logger_ap.error("Cannot save user message: Database module or add_message_to_history function unavailable.")
            except Exception as serialize_err:
                logger_ap.error(f"Agent Processor: Failed to serialize or save user message for {user_id}: {serialize_err}", exc_info=True)
            logger_ap.info(f"Agent Processor: Saved initial user message and updated profile for user {user_id}.")
        except Exception as initial_save_err:
            logger_ap.error(f"Agent Processor: Failed to save initial user message/profile for user {user_id}: {initial_save_err}", exc_info=True)

    # --- Получение моделей и настроек ---
    # <<< ИЗМЕНЕНИЕ: Получаем СПИСКИ моделей >>>
    lite_models_list = dp.workflow_data.get("lite_models_list", [])
    pro_models_list = dp.workflow_data.get("pro_models_list", [])
    available_pro_functions = dp.workflow_data.get("available_pro_functions", {})
    max_pro_steps = dp.workflow_data.get("max_pro_steps", 10)

    # Проверка наличия Pro моделей (критично)
    if not pro_models_list: # <--- Проверяем список
        logger_ap.critical(f"Core Agent: Pro model list not found or empty in workflow_data for chat {chat_id}")
        return escape_markdown_v2("⚠️ Ошибка: Основная модель AI недоступна.")
    if not bot_instance: # (остается)
        logger_ap.critical(f"Core Agent: Bot instance unavailable.")
        return escape_markdown_v2("⚠️ Ошибка: Экземпляр бота недоступен.")

    # --- ОПРЕДЕЛЕНИЕ ПУТИ ОБРАБОТКИ (логика остается, но проверка Lite изменена) ---
    call_pro_directly = False
    call_lite_filter = False
    pro_reason = ""

    if force_pro_model:
        call_pro_directly = True
        pro_reason = "Forced by flag"
    elif chat_type == aiogram_ChatType.PRIVATE:
        call_pro_directly = True
        pro_reason = "Private chat"
    elif chat_type in {aiogram_ChatType.GROUP, aiogram_ChatType.SUPERGROUP}:
        bot_info = await _get_bot_info(bot_instance)
        is_reply_to_bot = False
        is_mention = False
        if bot_info and bot_info.id:
            if (message.reply_to_message
                    and message.reply_to_message.from_user
                    and message.reply_to_message.from_user.id == bot_info.id):
                is_reply_to_bot = True
            if BOT_INFO_CACHE["username_lower"]:
                mention_pattern = rf"(^|\s)@{re.escape(BOT_INFO_CACHE['username_lower'])}(?![a-zA-Z0-9_])"
                if re.search(mention_pattern, user_input, re.IGNORECASE):
                    is_mention = True

        if is_reply_to_bot or is_mention:
            call_pro_directly = True
            pro_reason = "Reply/Mention in group"
        else:
            # <<< ИЗМЕНЕНИЕ: Проверяем список Lite моделей >>>
            if lite_models_list:
                call_lite_filter = True
                logger_ap.info(f"Lite filter will be used for group message (user {user_id} chat {chat_id}).")
            else:
                call_pro_directly = True
                pro_reason = "Lite filter unavailable (no models)"
                logger_ap.warning(f"Lite models unavailable for group chat {chat_id}, falling back to Pro.")
    else:
        call_pro_directly = True
        pro_reason = f"Unhandled chat type: {chat_type}"
        logger_ap.warning(pro_reason + ". Proceeding with Pro model.")

    # --- ЛОГИКА ВЫЗОВОВ ---
    actions_from_lite: Optional[List[Dict]] = None
    trigger_pro_after_lite = False

    # --- 1. Вызов Lite-фильтра (если решили) ---
    if call_lite_filter:
        try:
            # <<< НОВОЕ: Выбор Lite модели по индексу >>>
            # Используем тот же индекс, что и для Pro, предполагая синхронную ротацию
            # или что Lite не делает вызовы к API, требующие отдельной ротации.
            current_lite_index = get_current_api_key_index(dp)
            if current_lite_index >= len(lite_models_list):
                logger_ap.warning(f"Lite index {current_lite_index} out of bounds, resetting to 0.")
                current_lite_index = 0
            lite_model_instance = lite_models_list[current_lite_index]
            logger_ap.debug(f"Using Lite model index {current_lite_index} for filter.")

            lite_input = f"user_id: {user_id}\nchat_id: {chat_id}\nuser_input: {user_input}"
            lite_response = await lite_model_instance.generate_content_async(lite_input)

            # --- Обработка ответа Lite (остается без изменений) ---
            parse_result = parse_lite_llm_response(lite_response.text)

            if isinstance(parse_result, str) and parse_result == "NO_ACTION_NEEDED":
                logger_ap.info(f"Lite filter determined NO_ACTION_NEEDED (user {user_id} chat {chat_id}).")
                return None
            elif isinstance(parse_result, list):
                actions_from_lite = parse_result
                logger_ap.info(f"Lite filter returned {len(actions_from_lite)} actions.")
                if any(action.get("function_name") == "trigger_pro_model_processing" for action in actions_from_lite):
                    trigger_pro_after_lite = True
                    pro_reason = "Lite filter requested Pro"
            elif isinstance(parse_result, dict) and "error" in parse_result:
                logger_ap.error(f"Lite filter parsing failed: {parse_result.get('message')}. Falling back to Pro.")
                call_pro_directly = True
                pro_reason = "Lite filter parsing error"
            else:
                 logger_ap.error(f"Unexpected result from parse_lite_llm_response. Falling back to Pro.")
                 call_pro_directly = True
                 pro_reason = "Lite filter unexpected result"

            # !!! ВАЖНО: НЕ инкрементируем API ключ после вызова Lite, т.к. он
            # вероятно не делает вызовы к API Gemini или не требует ротации.
            # Ротация ключа происходит внутри process_request для Pro модели.

        except IndexError: # Если lite_models_list пуст, несмотря на проверку выше
             logger_ap.error(f"Lite filter failed: Model list is empty.")
             call_pro_directly = True
             pro_reason = "Lite model list empty"
        except Exception as lite_err:
            logger_ap.error(f"Error calling/processing Lite model API: {lite_err}", exc_info=True)
            call_pro_directly = True
            pro_reason = "Lite API/processing error"

    # --- 2. Выполнение действий из Lite (remember_user_info) (остается без изменений) ---
    if actions_from_lite is not None:
        remember_action_found = False
        pro_action_found = False
        for action in actions_from_lite:
            func_name = action.get("function_name")
            args = action.get("arguments", {})
            if func_name == "remember_user_info":
                 remember_action_found = True
                 # ... (код вызова database.upsert_user_note) ...
                 if database and hasattr(database, 'upsert_user_note') and 'user_id' in args and 'info_category' in args and 'info_value' in args:
                     try:
                         await database.upsert_user_note(
                             user_id=args['user_id'], # Используем ID из аргументов Lite
                             info_category=args['info_category'],
                             value=args['info_value']
                             # merge_lists по умолчанию True
                         )
                         logger_ap.info(f"Lite filter executed: remember_user_info for user {args.get('user_id')}")
                     except Exception as db_err:
                         logger_ap.error(f"DB error during remember_user_info from Lite: {db_err}", exc_info=True)
                 elif not database or not hasattr(database, 'upsert_user_note'):
                      logger_ap.error("Cannot execute remember_user_info: DB module or upsert_user_note unavailable.")
                 else:
                      logger_ap.error("remember_user_info requested with missing args from Lite.")
            elif func_name == "trigger_pro_model_processing":
                 pro_action_found = True

        if remember_action_found and not pro_action_found and not trigger_pro_after_lite:
            logger_ap.info(f"Finished processing: Lite triggered only 'remember'. No Pro call needed (user {user_id} chat {chat_id}).")
            return None

    # --- 3. Вызов Pro-модели (если нужно) ---
    if call_pro_directly or trigger_pro_after_lite:
        if not pro_reason: pro_reason = "Fallback or Unknown"
        logger_ap.info(f"Proceeding with Pro model for user {user_id} chat {chat_id} (Reason: {pro_reason}).")
        try:
            # <<< ИЗМЕНЕНИЕ: Передаем списки моделей и dispatcher >>>
            return await _execute_pro_model_logic(
                message=message,
                pro_models_list=pro_models_list,
                lite_models_list=lite_models_list, # Передаем для полноты
                available_pro_functions=available_pro_functions,
                max_pro_steps=max_pro_steps,
                dispatcher=dp # Передаем dispatcher (импортирован как dp)
            )
        except Exception as pro_exec_err:
             logger_ap.error(f"Core Agent: Unhandled exception during _execute_pro_model_logic call for chat {chat_id}: {pro_exec_err}", exc_info=True)
             return escape_markdown_v2("Произошла внутренняя ошибка при обработке вашего запроса основной моделью.")

    # --- 4. Завершение (если не вызвали Pro и не вышли раньше) ---
    logger_ap.info(f"Finishing request for user {user_id} chat {chat_id} (no direct Pro call, Lite filter decided no further action/Pro needed).")
    return None

logger_ap.info("--- agent_processor.py loaded successfully. ---")