# ai_interface/openai_api.py
import logging
from typing import List, Dict, Any, Optional, Tuple, Union

# --- Зависимости OpenAI ---
dependencies_ok = True
try:
    from openai import AsyncOpenAI, RateLimitError, APIError, OpenAIError # Импортируем базовые и специфичные ошибки
    from openai.types.chat import ChatCompletion
except ImportError:
    logging.getLogger(__name__).critical("CRITICAL: 'openai' library not found. OpenAI functionality will be unavailable.")
    dependencies_ok = False
    # Заглушки для типов и ошибок, чтобы код мог быть загружен
    AsyncOpenAI = type('AsyncOpenAI', (object,), {}) # type: ignore
    ChatCompletion = type('ChatCompletion', (object,), {}) # type: ignore
    RateLimitError = type('RateLimitError', (Exception,), {}) # type: ignore
    APIError = type('APIError', (Exception,), {}) # type: ignore
    OpenAIError = type('OpenAIError', (Exception,), {}) # type: ignore

# Импортируем функцию очистки сообщений
try:
    from utils.message_utils import sanitize_openai_messages
except ImportError:
    logging.getLogger(__name__).warning("Could not import sanitize_openai_messages function, using fallback")
    def sanitize_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Заглушка для функции очистки сообщений"""
        return messages

logger = logging.getLogger(__name__)

async def call_openai_api(
    client: AsyncOpenAI,
    model: str,
    messages: List[Dict[str, Any]], # История в формате OpenAI
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Union[str, Dict]] = "auto", # "auto", "none", или {"type": "function", "function": {"name": "my_function"}}
    temperature: float = 0.7, # Пример параметра
    max_tokens: Optional[int] = None, # Пример параметра
    # ... другие параметры OpenAI по необходимости
    **kwargs # Для передачи дополнительных параметров, если они есть в config
) -> Tuple[Optional[ChatCompletion], Optional[str]]:
    """
    Выполняет вызов к OpenAI Chat Completion API.
    Обрабатывает основные ошибки API.

    Args:
        client: Экземпляр AsyncOpenAI.
        model: Имя модели OpenAI (e.g., "gpt-4o").
        messages: Список сообщений в формате OpenAI.
        tools: Список описаний инструментов (функций).
        tool_choice: Режим выбора инструмента.
        temperature: Температура генерации.
        max_tokens: Максимальное количество токенов в ответе.
        **kwargs: Дополнительные параметры для API.

    Returns:
        Tuple[Optional[ChatCompletion], Optional[str]]: Кортеж (объект ответа ChatCompletion, сообщение об ошибке)
    """
    if not dependencies_ok:
        return None, "OpenAI library is not available."
    if not client or not isinstance(client, AsyncOpenAI):
         return None, "Invalid or missing OpenAI client instance."
    if not model:
         return None, "OpenAI model name not specified."
    if not messages:
        return None, "Cannot call OpenAI API with empty message list."

    # ПРИНУДИТЕЛЬНАЯ очистка сообщений перед отправкой
    cleaned_messages = sanitize_openai_messages(messages)
    if len(cleaned_messages) != len(messages):
        logger.warning(f"Cleaned {len(messages) - len(cleaned_messages)} invalid messages before OpenAI API call")
        messages = cleaned_messages
        if not messages:
            return None, "All messages were invalid and removed before API call."

    logger.debug(f"Calling OpenAI API. Model: {model}, Messages: {len(messages)}, Tools: {'Yes' if tools else 'No'}, Temp: {temperature}")

    # Собираем параметры, исключая None
    api_params = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        api_params["tools"] = tools
        api_params["tool_choice"] = tool_choice
    if max_tokens is not None:
        api_params["max_tokens"] = max_tokens
    if kwargs:
        api_params.update(kwargs) # Добавляем любые другие переданные параметры

    try:
        response = await client.chat.completions.create(**api_params)

        # Базовое логирование ответа
        if response.choices:
            finish_reason = response.choices[0].finish_reason
            logger.debug(f"OpenAI API call successful. Finish reason: {finish_reason}")
        else:
            logger.warning("OpenAI API response has no choices.")

        return response, None

    except RateLimitError as e:
        error_msg = f"OpenAI Rate Limit Error: {e}"
        logger.warning(error_msg)
        # Возвращаем специфичную ошибку для обработки в вызывающем коде
        return None, f"RATE_LIMIT_ERROR: {e}"
    except APIError as e:
        error_msg = f"OpenAI API Error (status={e.status_code}): {e.message}"
        logger.error(error_msg, exc_info=True)
        return None, f"API_ERROR: {e.message}"
    except OpenAIError as e: # Ловим другие ошибки OpenAI
        error_msg = f"OpenAI Library Error: {e}"
        logger.error(error_msg, exc_info=True)
        return None, f"OPENAI_ERROR: {e}"
    except Exception as e:
        error_msg = f"Unexpected error during OpenAI API call: {e}"
        logger.error(error_msg, exc_info=True)
        return None, f"UNEXPECTED_ERROR: {e}"