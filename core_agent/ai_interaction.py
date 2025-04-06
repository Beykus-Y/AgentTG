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
    # <<< Импортируем функции управления индексом >>>
    from bot_lifecycle import get_current_api_key_index, increment_api_key_index
    # <<< Импортируем Dispatcher для доступа к workflow_data >>>
    from aiogram import Dispatcher
except ImportError:
    logging.critical("CRITICAL: Failed to import dependencies (ai_interface, bot_lifecycle, aiogram) in ai_interaction.", exc_info=True)
    gemini_api = None # type: ignore
    fc_processing = None # type: ignore
    Dispatcher = Any # type: ignore
    # Заглушки для функций управления индексом
    def get_current_api_key_index(*args, **kwargs) -> int: return 0
    def increment_api_key_index(*args, **kwargs) -> int: return 0
    # Заглушка для process_gemini_fc_cycle, чтобы код не падал
    async def mock_fc_cycle(*args, **kwargs): return None, None, None, None # Возвращает 4 значения
    if fc_processing: fc_processing.process_gemini_fc_cycle = mock_fc_cycle


# --- Типы Google и зависимости ---
try:
    import google.api_core.exceptions
    # <<< Прямой импорт Content из google.ai >>>
    from google.ai.generativelanguage import Content
    # <<< Импорт GenerateContentResponse из google.generativeai.types >>>
    from google.generativeai.types import GenerateContentResponse
    # Тип ChatType для аннотации (опционально, можно использовать Any)
    from aiogram.enums import ChatType
except ImportError:
    logging.warning("Could not import specific Google/Aiogram types in ai_interaction.")
    Content = Any
    GenerateContentResponse = Any
    ChatType = Any
    google = Any # Заглушка для exceptions

logger = logging.getLogger(__name__)

async def process_request(
    # <<< УДАЛЕНО: model_instance больше не передается напрямую >>>
    # model_instance: Any,
    initial_history: List[Content],
    user_input: str,
    available_functions: Dict[str, Callable],
    max_steps: int,
    chat_id: int,
    user_id: int,
    chat_type: ChatType, # Используем импортированный тип или Any
    # <<< ДОБАВЛЕНО: Передаем dispatcher для доступа к workflow_data >>>
    dispatcher: Dispatcher
) -> Tuple[Optional[List[Content]], Optional[str], Optional[str], Optional[str], Optional[Dict]]:
    """
    Запускает сессию Gemini, отправляет сообщение, обрабатывает FC
    и обрабатывает ошибки квоты (429) с ПЕРЕКЛЮЧЕНИЕМ КЛЮЧЕЙ/МОДЕЛЕЙ и повторными попытками.

    Args:
        initial_history: Начальная история чата (список объектов Content).
        user_input: Текст сообщения пользователя.
        available_functions: Словарь доступных функций.
        max_steps: Максимальное количество шагов FC.
        chat_id: ID чата.
        user_id: ID пользователя.
        chat_type: Тип чата.
        dispatcher: Экземпляр Dispatcher для доступа к workflow_data.

    Returns:
        - final_history_obj_list: Финальная история (список объектов Content) или None при ошибке.
        - error_message: Сообщение об ошибке, если она произошла.
        - last_called_func_name: Имя последней *успешно* вызванной функции.
        - last_sent_text: Текст последнего сообщения, отправленного через send_telegram_message (или None).
        - last_func_result: Результат (словарь) последнего успешного вызова (или None).
    """
    # --- Инициализация возвращаемых значений ---
    final_history_obj_list: Optional[List[Content]] = None
    last_called_func_name: Optional[str] = None
    error_message: Optional[str] = None
    last_sent_text: Optional[str] = None
    last_func_result: Optional[Dict] = None

    logger.info(f"Running Gemini interaction for chat={chat_id}, user={user_id}, chat_type={chat_type}")

    # --- Проверки на старте ---
    if not gemini_api or not fc_processing:
         error_message = "Internal configuration error: AI interface modules not loaded."
         logger.critical(error_message)
         return None, error_message, None, None, None
    if dispatcher is None:
         error_message = "Internal configuration error: Dispatcher instance not provided."
         logger.critical(error_message)
         return None, error_message, None, None, None

    # <<< НОВОЕ: Получаем СПИСКИ моделей из workflow_data >>>
    pro_models_list = dispatcher.workflow_data.get("pro_models_list")
    api_keys_list = dispatcher.workflow_data.get("google_api_keys", []) # Получаем ключи для логирования

    # Проверяем наличие и непустоту списка моделей
    if not pro_models_list or not isinstance(pro_models_list, list) or len(pro_models_list) == 0:
         error_message = "AI Pro model list is not available or empty in workflow_data."
         logger.critical(f"{error_message} Chat: {chat_id}")
         return None, error_message, None, None, None

    num_keys = len(pro_models_list)
    logger.debug(f"Found {num_keys} Pro models (API keys) to use.")

    # --- НАСТРОЙКИ RETRY/ПЕРЕКЛЮЧЕНИЯ ---
    MAX_KEY_SWITCH_RETRIES = num_keys # Попробуем каждый ключ по одному разу
    INITIAL_RETRY_DELAY_SECONDS = 2 # Задержка ПЕРЕД повторной попыткой с НОВЫМ ключом

    # --- Инициализация переменных для цикла ---
    model_instance: Optional[Any] = None # Модель текущей попытки
    chat_session: Optional[Any] = None   # Сессия текущей попытки
    current_response: Optional[GenerateContentResponse] = None # Ответ текущей попытки
    retries = 0                          # Счетчик попыток (переключений ключей)
    initial_key_index = get_current_api_key_index(dispatcher) # Запоминаем стартовый индекс
    current_key_index = initial_key_index   # Индекс для текущей попытки

    # --- Основной цикл выбора ключа и попытки API вызова ---
    while retries < MAX_KEY_SWITCH_RETRIES: # Изменено условие цикла
        # 1. Выбираем модель для текущей попытки
        try:
            model_instance = pro_models_list[current_key_index]
            current_key_snippet = f"...{api_keys_list[current_key_index][-4:]}" if current_key_index < len(api_keys_list) else "???"
            logger.info(f"Attempt {retries + 1}/{MAX_KEY_SWITCH_RETRIES}. Using API key index {current_key_index} ({current_key_snippet}) for chat {chat_id}")
        except IndexError:
             # Этого не должно произойти при правильной логике % num_keys, но добавим защиту
             logger.error(f"Logic Error: Invalid API key index {current_key_index} attempted (list size {num_keys}). Resetting to 0.")
             current_key_index = 0
             if num_keys > 0: model_instance = pro_models_list[0]
             else: # Если список моделей внезапно стал пустым
                 error_message = "AI Pro model list became empty during processing."
                 logger.critical(error_message)
                 return None, error_message, None, None, None
        except Exception as model_select_err:
             error_message = f"Failed to select model for key index {current_key_index}: {model_select_err}"
             logger.critical(error_message, exc_info=True)
             # Инкрементируем индекс перед выходом, чтобы след. запрос начал с другого ключа
             increment_api_key_index(dispatcher)
             return None, error_message, None, None, None

        # 2. Создаем сессию для ВЫБРАННОЙ модели
        try:
             chat_session = model_instance.start_chat(history=initial_history)
             if not chat_session: raise ValueError("model.start_chat returned None")
             logger.debug(f"Chat session started successfully for key index {current_key_index}.")
        except Exception as session_err:
             error_message = f"Failed to start chat session for key index {current_key_index}: {session_err}"
             logger.error(f"{error_message} Chat: {chat_id}", exc_info=True)
             # Ошибка сессии - переходим к следующему ключу
             retries += 1
             current_key_index = (initial_key_index + retries) % num_keys # Переключаем индекс
             logger.warning(f"Session error. Will try next key index {current_key_index} after delay.")
             await asyncio.sleep(INITIAL_RETRY_DELAY_SECONDS) # Задержка перед след. ключом
             continue # Переходим к следующей итерации цикла while

        # 3. Пытаемся выполнить API вызов с текущей моделью/сессией
        try:
             loop = asyncio.get_running_loop()
             logger.debug(f"Attempting Gemini API call with key index {current_key_index} for chat {chat_id}")

             # --- Вызов синхронной функции в executor'е ---
             current_response = await loop.run_in_executor(
                 None, # Используем executor по умолчанию
                 gemini_api.send_message_to_gemini, # Имя синхронной функции
                 model_instance, # 1-й аргумент (model)
                 chat_session,   # 2-й аргумент (chat_session)
                 user_input      # 3-й аргумент (user_message)
             )

             # --- Логирование ответа (можно добавить детализацию, как было раньше) ---
             if current_response:
                  logger.debug(f"Raw Gemini Response object (key index {current_key_index}) for chat {chat_id}: {current_response!r}")
                  # ... (можно добавить код для логирования parts, text, function_call и т.д.) ...
             else:
                  # Случай, когда send_message_to_gemini вернула None без исключения
                  logger.error(f"Gemini API call with key index {current_key_index} returned None response.")
                  # Можно либо пробовать следующий ключ, либо вернуть ошибку
                  # Давайте попробуем следующий ключ
                  raise google.api_core.exceptions.Unknown("API returned None response") # Имитируем ошибку для перехода


             # --- Успех! Выходим из цикла retry ---
             logger.info(f"API call successful with key index {current_key_index} for chat {chat_id}")
             # !!! ВАЖНО: Сдвигаем ГЛОБАЛЬНЫЙ индекс для СЛЕДУЮЩЕГО запроса к боту !!!
             increment_api_key_index(dispatcher)
             break # <--- Выход из цикла while

        # --- Ловим ошибку квоты (429) ---
        except google.api_core.exceptions.ResourceExhausted as quota_error:
             logger.warning(f"Quota exceeded (429) on key index {current_key_index} for chat {chat_id}.")
             retries += 1
             if retries < MAX_KEY_SWITCH_RETRIES: # Если еще есть ключи для пробы
                 next_try_key_index = (initial_key_index + retries) % num_keys
                 logger.warning(f"Switching to next key index {next_try_key_index}. Retrying in {INITIAL_RETRY_DELAY_SECONDS}s... (Attempt {retries + 1}/{MAX_KEY_SWITCH_RETRIES})")
                 current_key_index = next_try_key_index # Устанавливаем индекс для следующей итерации
                 await asyncio.sleep(INITIAL_RETRY_DELAY_SECONDS) # Задержка перед след. попыткой
                 chat_session = None # Сбрасываем сессию, т.к. будем использовать другую модель
                 continue # Переходим к следующей итерации цикла while
             else: # Исчерпаны все ключи
                  error_message = f"Quota limit exceeded after trying all {num_keys} API keys."
                  logger.error(f"{error_message} Chat: {chat_id}. Last error on index {current_key_index}: {quota_error}")
                  # Инкрементируем индекс перед выходом
                  increment_api_key_index(dispatcher)
                  return None, error_message, None, None, None

        # --- Ловим другие ошибки API ---
        except Exception as api_call_error:
             # Ловим остальные ошибки, включая имитированную "API returned None response"
             error_message = f"API call failed on key index {current_key_index}: {api_call_error}"
             logger.error(f"{error_message} Chat: {chat_id}", exc_info=isinstance(api_call_error, google.api_core.exceptions.Unknown)) # Не логируем полный трейсбек для имитированной ошибки
             # При других ошибках НЕ переключаем ключ автоматически в этом же запросе,
             # но инкрементируем глобальный индекс для СЛЕДУЮЩЕГО запроса.
             increment_api_key_index(dispatcher)
             return None, error_message, None, None, None
    # --- КОНЕЦ ЦИКЛА while retries < MAX_KEY_SWITCH_RETRIES ---

    # --- Проверки после цикла ---
    if current_response is None:
        # Сюда можно попасть, если все ключи дали ошибку СЕССИИ (не API)
        if not error_message: error_message = "Failed to get response from AI model after trying all keys (possibly session errors)."
        logger.error(f"{error_message} Chat: {chat_id}")
        # Инкрементируем индекс перед выходом
        increment_api_key_index(dispatcher)
        return None, error_message, None, None, None
    if chat_session is None:
         # Этого не должно произойти, если current_response не None, но проверим
         error_message = "Internal logic error: Successful response but no valid chat session."
         logger.critical(f"{error_message} Chat: {chat_id}")
         # Инкрементируем индекс перед выходом
         increment_api_key_index(dispatcher)
         return None, error_message, None, None, None
    if model_instance is None:
         # Тоже маловероятно
         error_message = "Internal logic error: Successful response but no valid model instance."
         logger.critical(f"{error_message} Chat: {chat_id}")
         # Инкрементируем индекс перед выходом
         increment_api_key_index(dispatcher)
         return None, error_message, None, None, None


    # Проверка импорта типа Content (на всякий случай)
    if Content is None or not callable(fc_processing.process_gemini_fc_cycle):
        error_message = "Internal configuration error: AI type system or FC processor failed."
        logger.critical(error_message)
        # Инкрементируем индекс перед выходом
        increment_api_key_index(dispatcher)
        return None, error_message, None, None, None

    # --- Запускаем цикл обработки Function Calling ---
    # Используем model_instance и chat_session, полученные на ПОСЛЕДНЕЙ УСПЕШНОЙ итерации
    try:
        final_history_obj_list, last_called_func_name, last_sent_text, last_func_result = await fc_processing.process_gemini_fc_cycle(
            model_instance=model_instance, # Успешная модель
            chat_session=chat_session,     # Успешная сессия
            available_functions_map=available_functions,
            max_steps=max_steps,
            original_chat_id=chat_id,
            original_user_id=user_id
        )
    except Exception as fc_error:
        error_message = f"Error during Function Calling processing: {fc_error}"
        logger.error(f"{error_message} Chat: {chat_id}", exc_info=True)
        # Неясно, нужно ли инкрементировать индекс здесь, т.к. основной вызов API прошел.
        # Пока не будем инкрементировать повторно.
        # final_history_obj_list будет None в этом случае по логике process_gemini_fc_cycle
        return getattr(chat_session, 'history', None), error_message, None, None, None # Возвращаем историю до ошибки FC

    # --- Обработка результата FC цикла ---
    # final_history_obj_list может быть None, если сам цикл FC упал критически (обработано выше)
    if final_history_obj_list is None:
         # Эта ветка достигается, если process_gemini_fc_cycle вернул (None, ...)
         if not error_message: # Если ошибка не была установлена внутри fc_cycle
              error_message = "AI model processing (Function Calling) cycle failed."
         logger.error(f"{error_message} Chat: {chat_id}")
         # Возвращаем имя последней функции, если оно было установлено до ошибки
         # Историю берем из chat_session, т.к. final_history_obj_list тут None
         return getattr(chat_session, 'history', None), error_message, last_called_func_name, last_sent_text, last_func_result
    elif not final_history_obj_list:
         # Случай, когда fc_cycle вернул ([], ...) - пустой список
         error_message = "AI model processing cycle resulted in empty history (unexpected)."
         logger.warning(f"{error_message} Chat: {chat_id}")
         return None, error_message, last_called_func_name, last_sent_text, last_func_result

    # Если все прошло успешно (API вызов + FC цикл)
    return final_history_obj_list, error_message, last_called_func_name, last_sent_text, last_func_result
