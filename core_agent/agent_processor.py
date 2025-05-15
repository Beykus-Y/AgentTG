# core_agent/agent_processor.py

import logging
import json
# --- Удален импорт RE ---
from typing import Optional, Dict, Any, List, Callable

# --- Aiogram и зависимости ---
try:
    from aiogram import types, Bot, Dispatcher, F
    from aiogram.enums import ChatType as aiogram_ChatType
    # --- <<< ИЗМЕНЕНО: Импортируем escape_markdown_v2 из utils.helpers >>> ---
    from utils.helpers import escape_markdown_v2, remove_markdown, is_admin as check_if_admin
    from bot_loader import dp, bot as bot_instance
    import database
    from database.crud_ops.profiles import upsert_user_profile
    from .history_manager import prepare_history, save_history
    from .ai_interaction import process_request
    from .response_parsers import parse_lite_llm_response
    from bot_lifecycle import get_current_api_key_index # Только для Google Lite
    from ai_interface import gemini_api, openai_api # Импортируем оба
    from .result_parser import extract_text as extract_gemini_text # Для Gemini
    dependencies_ok = True
except ImportError as e:
    logging.getLogger(__name__).critical(f"CRITICAL: Failed import dependencies agent_processor: {e}", exc_info=True)
    dependencies_ok = False
    # Заглушки
    types = Any; Bot = Any; Dispatcher = Any; F = Any; aiogram_ChatType = Any;
    def escape_markdown_v2(text: Optional[str]) -> str: return text or ""
    def remove_markdown(text: Optional[str]) -> str: return text or ""
    def check_if_admin(uid: Optional[int]) -> bool: return False
    dp = type('obj', (object,), {'workflow_data': {}})(); bot_instance = None # type: ignore
    database = None # type: ignore
    async def upsert_user_profile(*args, **kwargs): pass
    async def prepare_history(*args, **kwargs): return [], 0
    async def save_history(*args, **kwargs): pass
    async def process_request(*args, **kwargs): return None, "Dep Error", None, None, None
    def parse_lite_llm_response(*args, **kwargs): return {"error": "Dep Error"}
    def get_current_api_key_index(*args, **kwargs): return 0
    gemini_api = None; openai_api = None
    def extract_gemini_text(*args, **kwargs): return None

logger_ap = logging.getLogger(__name__)
logger_ap.info(f"--- Loading agent_processor.py (Dependencies OK: {dependencies_ok}) ---")

# --- Кэш информации о боте (без изменений) ---
BOT_INFO_CACHE: Dict[str, Any] = {"info": None, "username_lower": None}
async def _get_bot_info(bot_to_use: Bot) -> Optional[types.User]:
    global BOT_INFO_CACHE
    if BOT_INFO_CACHE["info"] is None and bot_to_use:
        try:
            bot_user = await bot_to_use.get_me()
            BOT_INFO_CACHE["info"] = bot_user
            BOT_INFO_CACHE["username_lower"] = bot_user.username.lower() if bot_user.username else None
            logger_ap.info(f"Bot info cached: ID={bot_user.id}, Username=@{bot_user.username}")
        except Exception as e:
            logger_ap.error(f"Failed get bot info: {e}"); BOT_INFO_CACHE = {"info": None, "username_lower": None}
    return BOT_INFO_CACHE["info"]

# --- Адаптированная вспомогательная функция для вызова Pro-модели ---
async def _execute_pro_model_logic(
    message: types.Message,
    available_pro_functions: Dict[str, Callable],
    max_pro_steps: int,
    dispatcher: Dispatcher
) -> Optional[str]:
    """
    Выполняет логику обработки Pro моделью (Google или OpenAI).
    Возвращает текст для ответа пользователю ИЛИ УЖЕ ЭКРАНИРОВАННОЕ сообщение об ошибке.
    """
    chat_id=message.chat.id
    user_id=message.from_user.id if message.from_user else 0
    chat_type=message.chat.type
    user_input=message.text or ""
    add_user_context = True

    ai_provider = dispatcher.workflow_data.get("ai_provider")
    if not ai_provider:
        logger_ap.critical("AI provider not found for Pro logic.")
        return escape_markdown_v2("⚠️ Ошибка конфигурации AI.") # Экранируем сразу

    logger_ap.debug(f"Executing Pro model ({ai_provider.upper()}) logic chat {chat_id}.")

    try:
        # 1. Подготовка истории
        initial_history_list, original_db_len = await prepare_history(
            chat_id=chat_id, user_id=user_id, chat_type=chat_type,
            ai_provider=ai_provider, add_notes=add_user_context, add_recent_logs=True
        )
        if not isinstance(initial_history_list, list):
             logger_ap.error(f"prepare_history failed chat {chat_id} ({ai_provider}).");
             return escape_markdown_v2("⚠️ Ошибка подготовки истории.") # Экранируем

        # 2. Взаимодействие с AI
        final_history_list, interaction_error_msg, last_func_name, last_sent_text, last_func_result = await process_request(
            initial_history=initial_history_list, user_input=user_input,
            available_functions=available_pro_functions, max_steps=max_pro_steps,
            chat_id=chat_id, user_id=user_id, chat_type=chat_type, dispatcher=dispatcher
        )

        # 3. Обработка результата
        final_response_text_escaped: Optional[str] = None

        if interaction_error_msg:
            logger_ap.error(f"Core Agent ({ai_provider.upper()}): AI interaction failed chat {chat_id}: {interaction_error_msg}")
            # <<< ИЗМЕНЕНО: Формируем и экранируем сообщение об ошибке здесь >>>
            final_response_text_escaped = f"Произошла ошибка при обработке \\({ai_provider}\\): {escape_markdown_v2(interaction_error_msg)}"

        elif final_history_list:
            # Извлечение текста
            final_response_text_raw = None
            if ai_provider == 'openai':
                 if final_history_list and isinstance(final_history_list[-1], dict):
                      last_message = final_history_list[-1]
                      if last_message.get('role') == 'assistant' and isinstance(last_message.get('content'), str):
                           final_response_text_raw = last_message.get('content')
            elif ai_provider == 'google':
                 final_response_text_raw = extract_gemini_text(final_history_list)

            # Подавление текста и экранирование
            if last_func_name == 'send_telegram_message' and final_response_text_raw:
                logger_ap.info(f"Suppressing final text output after send_telegram_message. Chat: {chat_id}")
            elif final_response_text_raw:
                logger_ap.info(f"Core Agent ({ai_provider.upper()}): Final text generated chat {chat_id}.")
                final_response_text_escaped = escape_markdown_v2(final_response_text_raw) # <<< Экранируем успешный ответ
            else:
                logger_ap.info(f"Core Agent ({ai_provider.upper()}): No final text to send chat {chat_id}.")

            # Сохранение истории
            if save_history:
                 await save_history(
                       chat_id=chat_id, final_history=final_history_list,
                       original_db_history_len=original_db_len, current_user_id=user_id,
                       ai_provider=ai_provider
                 )
            else: logger_ap.error(f"save_history function unavailable.")

        else: # None history без ошибки
            logger_ap.error(f"Core Agent ({ai_provider.upper()}): AI returned None/empty history chat {chat_id}")
            final_response_text_escaped = escape_markdown_v2(f"⚠️ Модель AI ({ai_provider}) не вернула результат.") # Экранируем

        # Возвращаем ТОЛЬКО текст (успешный или ошибка), готовый к отправке
        return final_response_text_escaped

    except Exception as pro_err:
        logger_ap.error(f"Unexpected error Pro logic ({ai_provider.upper()}): {pro_err}", exc_info=True)
        # <<< ИЗМЕНЕНО: Экранируем сообщение об ошибке здесь >>>
        return escape_markdown_v2(f"⚠️ Произошла внутренняя ошибка при обработке ({ai_provider}).")


# --- Основная функция обработчика ---
async def handle_user_request(
    message: types.Message,
    force_pro_model: bool = False
) -> Optional[str]:
    """
    Основная точка входа для обработки запроса.
    Возвращает текст для ответа пользователю или None.
    Сообщения об ошибках возвращаются уже экранированными для MarkdownV2.
    """
    if not dependencies_ok:
        return escape_markdown_v2("⚠️ Ошибка: Не загружены компоненты.") # Экранируем

    chat_id = message.chat.id; user = message.from_user
    user_id = user.id if user else 0; chat_type = message.chat.type
    user_input = message.text or ""

    if user_id == 0: return None # Игнор

    # Получаем провайдера
    ai_provider = dp.workflow_data.get("ai_provider")
    if not ai_provider: return escape_markdown_v2("⚠️ Ошибка: AI провайдер не настроен.") # Экранируем

    logger_ap.info(f"Core Agent ({ai_provider.upper()}): Handling request user={user_id} chat={chat_id}")

    # Сохранение профиля пользователя (но не сообщения, чтобы избежать дублирования)
    if user and database and hasattr(database, 'upsert_user_profile'):
        try:
            await upsert_user_profile(user_id=user.id, username=user.username, first_name=user.first_name, last_name=user.last_name)
            logger_ap.info(f"Agent Processor: Updated profile for user {user_id}.")
        except Exception as initial_save_err:
            logger_ap.error(f"Agent Processor: Failed to update profile for user {user_id}: {initial_save_err}", exc_info=True)
            # Не прерываем выполнение из-за ошибки сохранения
    
    # Удаляем прямое сохранение сообщения, так как оно будет сохранено через save_history позже
    # Сообщение пользователя сохраняется как часть истории после процессинга через save_history
    # Это позволяет избежать дублирования

    # Получение настроек и доступности моделей
    lite_model_available = False; pro_model_available = False
    if ai_provider == 'google':
        lite_model_available = bool(dp.workflow_data.get("lite_models_list"))
        pro_model_available = bool(dp.workflow_data.get("pro_models_list"))
    elif ai_provider == 'openai':
        lite_model_available = bool(dp.workflow_data.get("lite_openai_model")) and bool(dp.workflow_data.get("openai_client"))
        pro_model_available = bool(dp.workflow_data.get("pro_openai_model")) and bool(dp.workflow_data.get("openai_client"))
    available_pro_functions = dp.workflow_data.get("available_pro_functions", {})
    max_pro_steps = dp.workflow_data.get("max_pro_steps", 10)

    # Проверка Pro модели/клиента
    if not pro_model_available:
        return escape_markdown_v2(f"⚠️ Ошибка: Модель Pro AI ({ai_provider}) недоступна.") # Экранируем
    if not bot_instance:
        return escape_markdown_v2("⚠️ Ошибка: Экземпляр бота недоступен.") # Экранируем

    # Определение пути обработки
    call_pro_directly = False; call_lite_filter = False; pro_reason = ""
    if force_pro_model: call_pro_directly = True; pro_reason = "Forced"
    elif chat_type == aiogram_ChatType.PRIVATE: call_pro_directly = True; pro_reason = "Private"
    elif chat_type in {aiogram_ChatType.GROUP, aiogram_ChatType.SUPERGROUP}:
        bot_info = await _get_bot_info(bot_instance)
        is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user and bot_info and message.reply_to_message.from_user.id == bot_info.id
        is_mention = bot_info and BOT_INFO_CACHE["username_lower"] and f'@{BOT_INFO_CACHE["username_lower"]}' in user_input.lower()
        if is_reply_to_bot or is_mention: call_pro_directly = True; pro_reason = "Reply/Mention"
        elif lite_model_available: call_lite_filter = True
        else: call_pro_directly = True; pro_reason = f"Lite N/A ({ai_provider})"
    else: call_pro_directly = True; pro_reason = f"ChatType {chat_type}"

    # Вызов Lite-фильтра
    actions_from_lite: Optional[List[Dict]] = None
    trigger_pro_after_lite = False
    if call_lite_filter:
        try:
            lite_response_text = None; lite_input = f"..." # Формируем ввод
            logger_ap.info(f"Calling Lite filter ({ai_provider.upper()})...")

            if ai_provider == 'google':
                 # ... (Логика вызова Google Lite API) ...
                 lite_models = dp.workflow_data.get("lite_models_list", []); current_index = get_current_api_key_index(dp)
                 if lite_models and current_index < len(lite_models) and gemini_api:
                     lite_model_instance = lite_models[current_index]
                     lite_response = await lite_model_instance.generate_content_async(lite_input)
                     lite_response_text = lite_response.text if lite_response else None
                 else: logger_ap.error(f"Google Lite model instance unavailable index {current_index}.")

            elif ai_provider == 'openai':
                 # ... (Логика вызова OpenAI Lite API) ...
                 client = dp.workflow_data.get("openai_client"); lite_model = dp.workflow_data.get("lite_openai_model")
                 lite_prompt = dp.workflow_data.get("lite_system_prompt")
                 if client and lite_model and openai_api:
                      lite_messages = [{"role": "system", "content": lite_prompt}] if lite_prompt else []
                      lite_messages.append({"role": "user", "content": lite_input})
                      openai_response, error = await openai_api.call_openai_api(client, lite_model, lite_messages, tools=None, temperature=0.1)
                      if openai_response and openai_response.choices: lite_response_text = openai_response.choices[0].message.content
                      elif error: logger_ap.error(f"OpenAI Lite API failed: {error}")
                 else: logger_ap.error("OpenAI client/lite model unavailable for filter.")

            # Обработка ответа Lite
            if lite_response_text:
                 parse_result = parse_lite_llm_response(lite_response_text)
                 # ... (Логика обработки parse_result как раньше) ...
                 if isinstance(parse_result, str) and parse_result == "NO_ACTION_NEEDED": return None
                 elif isinstance(parse_result, list): actions_from_lite = parse_result; trigger_pro_after_lite = any(a.get("function_name") == "trigger_pro_model_processing" for a in actions_from_lite)
                 else: call_pro_directly = True; pro_reason = "Lite parse error"
            else: call_pro_directly = True; pro_reason = "Lite API/proc error"

        except Exception as lite_err:
            logger_ap.error(f"Error Lite filter ({ai_provider}): {lite_err}", exc_info=True)
            call_pro_directly = True; pro_reason = "Lite Exception"

    # Выполнение действий из Lite
    if actions_from_lite is not None:
        # ... (Логика вызова remember_user_info как раньше) ...
        remember_action_found = False; pro_action_found = False
        for action in actions_from_lite:
             func_name = action.get("function_name"); args = action.get("arguments", {})
             if func_name == "remember_user_info": remember_action_found = True # ... (вызов DB) ...
             elif func_name == "trigger_pro_model_processing": pro_action_found = True
        if remember_action_found and not pro_action_found and not trigger_pro_after_lite:
            logger_ap.info("Finished: Lite triggered only 'remember'."); return None

    # Вызов Pro-модели
    if call_pro_directly or trigger_pro_after_lite:
        logger_ap.info(f"Proceeding with Pro model ({ai_provider.upper()}) Reason: {pro_reason}")
        # Вызываем _execute_pro_model_logic, она вернет текст или экранированную ошибку
        return await _execute_pro_model_logic(
            message=message,
            available_pro_functions=available_pro_functions,
            max_pro_steps=max_pro_steps,
            dispatcher=dp
        )

    # Завершение
    logger_ap.info(f"Finishing request ({ai_provider.upper()}) (no Pro call needed).")
    return None # Если не вызвали Pro и не вышли раньше

logger_ap.info(f"--- agent_processor.py loaded successfully. ---")