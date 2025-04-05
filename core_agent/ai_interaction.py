# core_agent/ai_interaction.py

import asyncio
import logging
import json
from typing import Dict, Any, List, Optional, Tuple, Callable

# --- Локальные импорты ---
try:
    # Модули для взаимодействия с AI и обработки FC
    import google.api_core.exceptions
    from ai_interface import gemini_api, fc_processing
except ImportError:
    logging.critical("CRITICAL: Failed to import ai_interface modules in ai_interaction.", exc_info=True)
    gemini_api = None # type: ignore
    fc_processing = None # type: ignore
    # Заглушка для process_gemini_fc_cycle, чтобы код не падал
    async def mock_fc_cycle(*args, **kwargs): return None, None
    if fc_processing: fc_processing.process_gemini_fc_cycle = mock_fc_cycle

# --- Типы Google и зависимости ---
try:
    import google.api_core.exceptions
    from google.ai.generativelanguage import Content # Нужен для аннотации возвращаемого типа
    from aiogram.enums import ChatType # Нужен для аннотации
except ImportError:
    logging.warning("Could not import specific Google/Aiogram types in ai_interaction.")
    Content = Any
    ChatType = Any
    google = Any # Заглушка для exceptions

logger = logging.getLogger(__name__)

async def process_request(
    model_instance: Any,
    initial_history: List[Content], # История как список словарей
    user_input: str,
    available_functions: Dict[str, Callable],
    max_steps: int,
    chat_id: int,
    user_id: int,
    chat_type: ChatType # Добавляем тип чата для контекста
) -> Tuple[Optional[List[Content]], Optional[str], Optional[str], Optional[str], Optional[Dict]]:
    """
    Запускает сессию Gemini, отправляет сообщение, обрабатывает FC
    и обрабатывает ошибки квоты (429) с повторными попытками.

    Args:
        model_instance: Экземпляр модели Gemini (genai.GenerativeModel).
        initial_history: Начальная история чата в виде списка словарей.
        user_input: Текст сообщения пользователя.
        available_functions: Словарь доступных функций для Function Calling.
        max_steps: Максимальное количество шагов FC.
        chat_id: ID чата.
        user_id: ID пользователя, инициировавшего запрос.
        chat_type: Тип чата (PRIVATE, GROUP, SUPERGROUP).

    Returns:
        - final_history_obj_list: Финальную историю (список объектов Content) или None при ошибке.
        - error_message: Сообщение об ошибке, если она произошла.
        - last_called_func_name: Имя последней *успешно* вызванной функции.
        - last_sent_text: Текст последнего сообщения, отправленного через send_telegram_message (или None).
        - last_func_result: Результат (словарь) последнего успешного вызова (или None).
    """
    logger.info(f"Running Gemini interaction for chat={chat_id}, current_user={user_id}, chat_type={chat_type}")
    final_history_obj_list: Optional[List[Content]] = None
    last_called_func_name: Optional[str] = None
    error_message: Optional[str] = None
    last_sent_text: Optional[str] = None
    last_func_result: Optional[Dict] = None

    # --- Проверки на старте ---
    if not gemini_api or not fc_processing:
         error_message = "Internal configuration error: AI interface modules not loaded."
         logger.critical(error_message)
         return None, error_message, None, None, None
    if not model_instance:
        error_message = "AI model instance is not available."
        logger.error(f"{error_message} Chat: {chat_id}")
        return None, error_message, None, None, None

    # --- НАСТРОЙКИ RETRY ---
    MAX_RETRIES = 2 # Макс. число повторных попыток при ошибке 429
    INITIAL_RETRY_DELAY_SECONDS = 3 # Начальная задержка
    RETRY_BACKOFF_FACTOR = 1.5 # Множитель для увеличения задержки
    # -----------------------

    try:
        
        # Запускаем сессию чата
        chat_session = model_instance.start_chat(history=initial_history)
        if not chat_session:
             raise ValueError("Failed to start chat session (model.start_chat returned None)")

        # --- ЦИКЛ С ПОВТОРНЫМИ ПОПЫТКАМИ ДЛЯ ПЕРВОГО ЗАПРОСА ---
        current_response = None
        retries = 0
        current_delay = INITIAL_RETRY_DELAY_SECONDS
        while retries <= MAX_RETRIES:
            try:
                 loop = asyncio.get_running_loop()
                 logger.debug(f"Attempting initial Gemini API call (Try {retries+1}/{MAX_RETRIES+1}) for chat {chat_id}")
                 # Вызываем синхронную функцию отправки в executor'е
                 current_response = await loop.run_in_executor(
                     None, gemini_api.send_message_to_gemini, model_instance, chat_session, user_input
                 )
                 # ----- НАЧАЛО ВСТАВКИ ЛОГИРОВАНИЯ -----
                 logger.debug(f"Raw Gemini Response object (try {retries+1}) for chat {chat_id}: {current_response!r}") # Используем current_response
                 try:
                     # Логируем основные атрибуты, если они есть
                     if hasattr(current_response, 'parts'):
                         logger.debug(f"Response parts (try {retries+1}) for chat {chat_id}: {current_response.parts}")
                     else:
                         logger.debug(f"Response (try {retries+1}) for chat {chat_id} has no 'parts'.")

                     if hasattr(current_response, 'text'):
                         logger.debug(f"Response text (try {retries+1}) for chat {chat_id}: '{current_response.text}'") # Добавил кавычки для ясности
                     else:
                         logger.debug(f"Response (try {retries+1}) for chat {chat_id} has no 'text'.")

                     # Проверяем parts на наличие function_calls, так как у response их может не быть напрямую
                     fc_found_in_parts = False
                     if hasattr(current_response, 'parts') and current_response.parts:
                          for part in current_response.parts:
                               if hasattr(part, 'function_call') and part.function_call:
                                    logger.debug(f"Response function_call in part (try {retries+1}) for chat {chat_id}: {part.function_call}")
                                    fc_found_in_parts = True
                     if not fc_found_in_parts:
                          logger.debug(f"Response (try {retries+1}) for chat {chat_id} has no function_calls in parts.")

                 except Exception as log_err:
                      logger.warning(f"Error during detailed response logging (try {retries+1}) for chat {chat_id}: {log_err}")
                 # ----- КОНЕЦ ВСТАВКИ ЛОГИРОВАНИЯ -----
                 # Если успешно, выходим из цикла retry
                 logger.debug(f"Initial API call successful for chat {chat_id}")
                 break
            # Ловим конкретную ошибку квоты
            except google.api_core.exceptions.ResourceExhausted as quota_error:
                 retries += 1
                 if retries <= MAX_RETRIES:
                     logger.warning(f"Quota exceeded (429) on initial call for chat {chat_id}. Retrying in {current_delay:.1f}s... (Attempt {retries}/{MAX_RETRIES})")
                     await asyncio.sleep(current_delay)
                     current_delay *= RETRY_BACKOFF_FACTOR # Увеличиваем задержку
                 else: # Исчерпаны попытки
                      error_message = f"Quota limit exceeded after {MAX_RETRIES+1} attempts on initial call. Please try again later."
                      logger.error(f"{error_message} Chat: {chat_id}. Last error: {quota_error}")
                      return None, error_message, None, None, None
            # Ловим другие возможные ошибки API
            except Exception as api_call_error:
                 error_message = f"Initial API call failed: {api_call_error}"
                 logger.error(f"{error_message} Chat: {chat_id}", exc_info=True)
                 return None, error_message, None, None, None
        # --- КОНЕЦ ЦИКЛА RETRY ДЛЯ ПЕРВОГО ЗАПРОСА ---

        # Если current_response все еще None после цикла (маловероятно, но для полноты)
        if current_response is None:
            if not error_message: # Если ошибка не была установлена ранее
                 error_message = "Failed to get initial response from AI model after retries."
                 logger.error(f"{error_message} Chat: {chat_id}")
            return None, error_message, None, None, None

        # Проверка импорта типа Content (на всякий случай)
        if Content is None or not callable(fc_processing.process_gemini_fc_cycle):
            error_message = "Internal configuration error: AI type system or FC processor failed."
            logger.critical(error_message)
            return None, error_message, None, None, None

        # --- Запускаем цикл обработки Function Calling ---
        # Передаем активную chat_session, которая будет обновляться внутри цикла
        final_history_obj_list, last_called_func_name, last_sent_text, last_func_result = await fc_processing.process_gemini_fc_cycle(
            model_instance=model_instance,
            chat_session=chat_session, # Передаем сессию
            available_functions_map=available_functions,
            max_steps=max_steps,
            original_chat_id=chat_id,
            original_user_id=user_id
        )

        # Обработка результата FC цикла
        if final_history_obj_list is None:
             error_message = "AI model processing (Function Calling) cycle failed critically."
             logger.error(f"{error_message} Chat: {chat_id}")
             # Возвращаем имя последней функции, если оно было установлено до ошибки
             return None, error_message, last_called_func_name, last_sent_text, last_func_result
        elif not final_history_obj_list:
             # Это странная ситуация, но возможная
             error_message = "AI model processing cycle resulted in empty history."
             logger.warning(f"{error_message} Chat: {chat_id}")
             return None, error_message, last_called_func_name, last_sent_text, last_func_result

    except ValueError as ve: # Ошибка старта сессии или другая ValueError
        error_message = f"Failed to initialize or run AI session: {ve}"
        logger.error(f"ValueError during Gemini interaction chat {chat_id}: {ve}", exc_info=True)
        return None, error_message, None, None, None
    except Exception as e: # Другие неожиданные ошибки
        error_message = f"An unexpected error occurred during AI interaction: {e}"
        logger.error(f"Unexpected error in process_request for chat {chat_id}: {e}", exc_info=True)
        return None, error_message, None, None, None

    # Возвращаем результат: финальную историю (список Content), сообщение об ошибке (если было), имя последней функции
    return final_history_obj_list, error_message, last_called_func_name, last_sent_text, last_func_result