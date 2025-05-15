# core_agent/ai_interaction.py

import asyncio
import logging
import json
from typing import Dict, Any, List, Optional, Tuple, Callable, Union, cast

# --- Локальные импорты ---
try:
    # Интерфейсы API
    from ai_interface import gemini_api, openai_api
    # Обработка инструментов/FC
    from ai_interface.tool_processing import process_google_fc_cycle, process_openai_tool_cycle
    # Управление ключами Google
    from bot_lifecycle import get_current_api_key_index, increment_api_key_index
    # Диспетчер для доступа к данным
    from aiogram import Dispatcher
    from aiogram.enums import ChatType # Для аннотации
    # Импорт утилиты для сообщений
    from utils.message_utils import sanitize_openai_messages
except ImportError as e:
    logging.getLogger(__name__).critical(f"CRITICAL: Failed to import dependencies in ai_interaction: {e}", exc_info=True)
    # Заглушки
    gemini_api = None; openai_api = None # type: ignore
    async def process_google_fc_cycle(*args, **kwargs): return None, None, None, None
    async def process_openai_tool_cycle(*args, **kwargs): return None, None, None, None
    def get_current_api_key_index(*args, **kwargs): return 0
    def increment_api_key_index(*args, **kwargs): return 0
    Dispatcher = Any; ChatType = Any # type: ignore
    def sanitize_openai_messages(messages): return messages

# --- Типы и исключения AI (для обработки ошибок) ---
try: import google.api_core.exceptions as google_exceptions
except ImportError: google_exceptions = None
try: from openai import RateLimitError as OpenAIRateLimitError, APIError as OpenAIAPIError, OpenAIError
except ImportError: OpenAIRateLimitError = None; OpenAIAPIError = None; OpenAIError = None # type: ignore

# --- Типы ответов AI (для аннотаций) ---
try: from google.generativeai.types import GenerateContentResponse as GoogleResponse
except ImportError: GoogleResponse = Any
try: from openai.types.chat import ChatCompletion as OpenAIResponse
except ImportError: OpenAIResponse = Any
# Тип Content для Google (для аннотации истории)
try: from google.ai.generativelanguage import Content as GoogleContent
except ImportError: GoogleContent = Any

logger = logging.getLogger(__name__)

async def process_request(
    initial_history: List[Any], # Тип зависит от провайдера (List[GoogleContent] или List[Dict])
    user_input: str,
    available_functions: Dict[str, Callable],
    max_steps: int,
    chat_id: int,
    user_id: int,
    chat_type: ChatType,
    dispatcher: Dispatcher # <<< Принимаем dispatcher
) -> Tuple[Optional[List[Any]], Optional[str], Optional[str], Optional[str], Optional[Dict]]:
    """
    Выполняет основной цикл взаимодействия с AI (Google или OpenAI), включая обработку инструментов.
    Обрабатывает ошибки квоты для Google с переключением ключей.

    Возвращает:
        - final_history: Финальная история (List[GoogleContent] или List[Dict]) или None.
        - error_message: Сообщение об ошибке или None.
        - last_called_func_name: Имя последней успешно вызванной функции.
        - last_sent_text: Текст последнего сообщения через send_telegram_message.
        - last_func_result: Результат последнего успешного вызова функции.
    """
    ai_provider = dispatcher.workflow_data.get("ai_provider")
    logger.info(f"Running AI interaction ({ai_provider.upper()}) for chat={chat_id}, user={user_id}")

    # --- Инициализация возвращаемых значений ---
    final_history: Optional[List[Any]] = None
    error_message: Optional[str] = None
    last_called_func_name: Optional[str] = None
    last_sent_text: Optional[str] = None
    last_func_result: Optional[Dict] = None

    # --- Проверки ---
    if not ai_provider:
        return None, "AI provider not configured in workflow data.", None, None, None
    if not available_functions:
        logger.warning("No available functions provided for AI interaction.")
        # Продолжаем без инструментов

    # ======================================
    # === ЛОГИКА ДЛЯ GOOGLE GEMINI ===
    # ======================================
    if ai_provider == "google":
        if not gemini_api or not google_exceptions:
             return None, "Google API module or exceptions not loaded.", None, None, None

        pro_models_list = dispatcher.workflow_data.get("pro_models_list", [])
        api_keys_list = dispatcher.workflow_data.get("google_api_keys", [])
        if not pro_models_list or not isinstance(pro_models_list, list):
             return None, "Google Pro model list is not available or empty.", None, None, None

        # --- Логика Retry для Google ---
        num_keys = len(pro_models_list)
        MAX_KEY_SWITCH_RETRIES = num_keys
        INITIAL_RETRY_DELAY_SECONDS = 2
        retries = 0
        initial_key_index = get_current_api_key_index(dispatcher)
        current_key_index = initial_key_index
        model_instance: Optional[Any] = None
        chat_session: Optional[Any] = None
        current_response: Optional[GoogleResponse] = None

        while retries < MAX_KEY_SWITCH_RETRIES:
            try: # Выбор модели и старт сессии
                model_instance = pro_models_list[current_key_index]
                current_key_snippet = f"...{api_keys_list[current_key_index][-4:]}" if current_key_index < len(api_keys_list) else "???"
                logger.info(f"Google Attempt {retries + 1}/{MAX_KEY_SWITCH_RETRIES}. Using key index {current_key_index} ({current_key_snippet})")

                chat_session = model_instance.start_chat(history=initial_history)
                if not chat_session: raise ValueError("start_chat returned None")
                logger.debug(f"Google chat session started successfully index {current_key_index}.")

            except (IndexError, ValueError, Exception) as session_err:
                error_message = f"Failed select model/start session Google key index {current_key_index}: {session_err}"
                logger.error(f"{error_message} Chat: {chat_id}", exc_info=True)
                retries += 1
                if retries < MAX_KEY_SWITCH_RETRIES:
                    current_key_index = (initial_key_index + retries) % num_keys
                    logger.warning(f"Session error. Will try next key {current_key_index} after delay.")
                    await asyncio.sleep(INITIAL_RETRY_DELAY_SECONDS)
                    continue
                else:
                    increment_api_key_index(dispatcher) # Инкрементируем перед выходом
                    return None, error_message, None, None, None

            try: # Вызов API
                loop = asyncio.get_running_loop()
                logger.debug(f"Attempting Google API call index {current_key_index} chat {chat_id}")
                current_response = await loop.run_in_executor(
                     None, gemini_api.send_message_to_gemini, model_instance, chat_session, user_input
                )
                if current_response is None: # send_message_to_gemini вернула None
                    raise google_exceptions.Unknown("API returned None response") if google_exceptions else Exception("API returned None response")

                # Успех API вызова!
                logger.info(f"Google API call successful index {current_key_index} chat {chat_id}")
                increment_api_key_index(dispatcher) # Сдвигаем глобальный индекс
                break # Выход из цикла retry

            except google_exceptions.ResourceExhausted as quota_error:
                 logger.warning(f"Quota exceeded (429) Google key index {current_key_index} chat {chat_id}.")
                 retries += 1
                 if retries < MAX_KEY_SWITCH_RETRIES:
                     next_try_key_index = (initial_key_index + retries) % num_keys
                     logger.warning(f"Switching to next key {next_try_key_index}. Retrying in {INITIAL_RETRY_DELAY_SECONDS}s...")
                     current_key_index = next_try_key_index
                     await asyncio.sleep(INITIAL_RETRY_DELAY_SECONDS)
                     chat_session = None # Сбрасываем сессию
                     continue # К следующей итерации retry
                 else: # Исчерпаны все ключи
                      error_message = f"Quota limit exceeded after trying all {num_keys} Google keys."
                      logger.error(f"{error_message} Chat: {chat_id}. Last error index {current_key_index}: {quota_error}")
                      increment_api_key_index(dispatcher) # Инкрементируем перед выходом
                      return None, error_message, None, None, None

            except Exception as api_call_error:
                 # Ловим остальные ошибки API, включая имитированную "API returned None response"
                 error_message = f"Google API call failed index {current_key_index}: {api_call_error}"
                 logger.error(f"{error_message} Chat: {chat_id}", exc_info=True)
                 increment_api_key_index(dispatcher) # Инкрементируем глобальный индекс для СЛЕДУЮЩЕГО запроса
                 return None, error_message, None, None, None
        # --- Конец цикла retry для Google ---

        # Проверки после цикла
        if current_response is None or chat_session is None or model_instance is None:
            if not error_message: error_message = "Failed get Google response after retries (session/internal errors)."
            logger.error(f"{error_message} Chat: {chat_id}")
            # increment_api_key_index(dispatcher) # Уже должно было быть вызвано при ошибке
            return None, error_message, None, None, None

        # --- Запуск цикла Function Calling для Google ---
        try:
            final_history, last_called_func_name, last_sent_text, last_func_result = await process_google_fc_cycle(
                model_instance=model_instance,
                chat_session=chat_session,
                available_functions_map=available_functions,
                max_steps=max_steps,
                original_chat_id=chat_id,
                original_user_id=user_id
            )
        except Exception as fc_error:
            error_message = f"Error during Google Function Calling processing: {fc_error}"
            logger.error(f"{error_message} Chat: {chat_id}", exc_info=True)
            # Возвращаем историю до ошибки FC
            return getattr(chat_session, 'history', None), error_message, None, None, None

    # ======================================
    # === ЛОГИКА ДЛЯ OPENAI ===
    # ======================================
    elif ai_provider == "openai":
        if not openai_api or OpenAIRateLimitError is None or OpenAIAPIError is None: # Проверяем импорты
            return None, "OpenAI API module or error types not loaded.", None, None, None

        # Получаем данные OpenAI из workflow_data
        client = dispatcher.workflow_data.get("openai_client")
        model_name = dispatcher.workflow_data.get("pro_openai_model") # Используем Pro модель
        tools = dispatcher.workflow_data.get("openai_tools")
        temperature = dispatcher.workflow_data.get("openai_temperature", 0.7)
        max_tokens = dispatcher.workflow_data.get("openai_max_tokens")
        # <<< ПОЛУЧАЕМ СИСТЕМНЫЙ ПРОМПТ ДЛЯ PRO >>>
        system_prompt_text = dispatcher.workflow_data.get("pro_system_prompt")

        if not client or not model_name:
            return None, "OpenAI client or model name not configured.", None, None, None

        # Подготовка messages для OpenAI
        # initial_history должна быть List[Dict[str, str]] вида {"role": "user/assistant", "content": "..."}
        messages: List[Dict[str, Any]] = []
        if system_prompt_text and isinstance(system_prompt_text, str) and system_prompt_text.strip():
            messages.append({"role": "system", "content": system_prompt_text.strip()})
        
        # Добавляем историю, предполагая, что она уже в правильном формате List[Dict]
        if isinstance(initial_history, list):
            messages.extend(initial_history)
        elif initial_history is not None: # Если история есть, но не список - это проблема
            logger.warning(f"OpenAI interaction: initial_history is not a list (type: {type(initial_history)}). History might be incorrect.")
            # Можно попытаться преобразовать, если известен формат, или просто проигнорировать

        if user_input: # Добавляем текущий ввод пользователя
            messages.append({"role": "user", "content": user_input})
        
        if not messages: # Если после всего этого messages пуст (например, нет системного промпта, истории и user_input)
            return None, "Cannot call OpenAI API with empty message list (after prep).", None, None, None

        # Валидируем сообщения перед отправкой в API
        messages = sanitize_openai_messages(messages)
        logger.debug(f"Sanitized {len(messages)} messages for OpenAI API.")
        
        # Вызываем OpenAI API
        if not openai_api:
            logger.critical("openai_api module unavailable for initial request.")
            return None, "OpenAI API module unavailable", None, None, None
        
        # --- Вызов API OpenAI (пока без сложного retry) ---
        try:
             # Вызываем функцию-обертку для API OpenAI
             response_obj, api_error_msg = await openai_api.call_openai_api(
                 client=client, model=model_name, messages=messages,
                 tools=tools, temperature=temperature, max_tokens=max_tokens
             )

             if api_error_msg:
                 # Обрабатываем специфичные ошибки OpenAI, если нужно (например, RateLimit для retry)
                 if "RATE_LIMIT_ERROR" in api_error_msg:
                      # TODO: Реализовать логику retry с задержкой для OpenAI, если нужно
                      logger.warning(f"OpenAI Rate Limit hit for chat {chat_id}. No retry implemented yet.")
                      error_message = f"OpenAI API limit reached. Please try again later. ({api_error_msg})"
                 else:
                      error_message = f"OpenAI API Error: {api_error_msg}"
                 # Возвращаем ошибку без истории, т.к. первый вызов не удался
                 return None, error_message, None, None, None

             if response_obj is None: # Не должно произойти, если нет api_error_msg, но проверим
                  return None, "OpenAI API call returned None unexpectedly.", None, None, None

             # --- Запуск цикла обработки Tools для OpenAI ---
             # Передаем response_obj в обработчик, т.к. он содержит первый ответ
             final_history, last_called_func_name, last_sent_text, last_func_result = await process_openai_tool_cycle(
                 client=client, model_name=model_name, initial_messages=messages,
                 first_response=response_obj, # Передаем первый ответ
                 tools=tools, available_functions_map=available_functions,
                 max_steps=max_steps, chat_id=chat_id, user_id=user_id,
                 temperature=temperature, max_tokens=max_tokens # Передаем параметры генерации
             )

        except Exception as openai_proc_error:
             error_message = f"Error during OpenAI processing: {openai_proc_error}"
             logger.error(f"{error_message} Chat: {chat_id}", exc_info=True)
             # Истории может не быть, если ошибка произошла до первого вызова API
             # или final_history не был присвоен в process_openai_tool_cycle
             return None, error_message, None, None, None

    # ======================================
    # === НЕИЗВЕСТНЫЙ ПРОВАЙДЕР ===
    # ======================================
    else:
        error_message = f"Unknown AI provider '{ai_provider}' encountered in process_request."
        logger.critical(error_message)
        return None, error_message, None, None, None

    # --- Обработка финального результата (независимо от провайдера) ---
    if error_message:
        # Если была ошибка на каком-то этапе, но есть финальная история (например, ошибка в FC)
        logger.error(f"AI Interaction finished with error: {error_message}. Returning history if available. Chat: {chat_id}")
        return final_history, error_message, last_called_func_name, last_sent_text, last_func_result
    elif final_history is None:
         # Не должно произойти, если нет error_message, но проверяем
         logger.error(f"AI Interaction finished without error, but final_history is None. Chat: {chat_id}")
         return None, "Internal error: AI processing resulted in None history.", None, None, None
    else:
         logger.info(f"AI Interaction ({ai_provider.upper()}) completed successfully. Chat: {chat_id}. Final history length: {len(final_history)}")
         return final_history, None, last_called_func_name, last_sent_text, last_func_result # Успех