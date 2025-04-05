# core_agent/result_parser.py
import logging
from typing import Optional, List, Any

try:
    from google.ai import generativelanguage as glm
    Content = glm.Content
    Part = glm.Part
except ImportError:
    Content, Part = Any, Any
    logging.getLogger(__name__).warning("Could not import Google types (Content, Part) in result_parser.")

logger = logging.getLogger(__name__)

def extract_text(final_history_obj_list: List[Content]) -> Optional[str]:
    """
    Извлекает текстовое содержимое из последнего сообщения модели в истории.

    Args:
        final_history_obj_list: Список объектов Content, представляющих историю диалога.

    Returns:
        Объединенный текст из последнего сообщения модели или None, если текста нет
        или последний элемент не является сообщением модели.
    """
    logger.debug(f"Attempting to extract text from final history list (length: {len(final_history_obj_list)}).")

    if not final_history_obj_list:
        logger.warning("Cannot extract text: final_history_obj_list is empty.")
        return None

    # Безопасно получаем последний элемент
    try:
        # Используем try-except на случай, если список не поддерживает индексацию,
        # хотя ожидается список Content объектов.
        last_entry = final_history_obj_list[-1]
    except IndexError:
        logger.warning("Cannot extract text: final_history_obj_list seems empty despite initial check or doesn't support indexing.")
        return None
    except TypeError:
         logger.warning(f"Cannot extract text: final_history_obj_list is not a list or indexable type ({type(final_history_obj_list)}).")
         return None

    # Проверяем роль
    if not hasattr(last_entry, 'role') or last_entry.role != 'model':
        # Если последний элемент - не от модели, текста там быть не должно по нашей логике
        logger.debug(f"Last entry in history is not from model (role: {getattr(last_entry, 'role', 'N/A')}). No text to extract.")
        return None

    # Проверяем наличие parts и итерируемость
    parts_iterable = getattr(last_entry, 'parts', None)
    if parts_iterable is None or not hasattr(parts_iterable, '__iter__'):
        logger.warning(f"Final model response (entry type: {type(last_entry)}) has no iterable 'parts' attribute.")
        # Пытаемся ли мы извлечь текст из самого объекта, если он строка? (Маловероятно для Content)
        if isinstance(last_entry, str):
             logger.warning("The last entry was unexpectedly a string. Returning it as text.")
             return last_entry # Возвращаем строку, если это возможно
        return None # Не можем обработать parts

    # Теперь безопасно итерируем по parts
    extracted_texts = []
    for part in parts_iterable:
        if hasattr(part, 'text') and part.text:
            logger.debug(f"Extracted text part: '{part.text[:50]}...'")
            extracted_texts.append(part.text)
        elif hasattr(part, 'function_call'):
            logger.debug("Ignoring function_call part.")
        elif hasattr(part, 'function_response'):
            logger.debug("Ignoring function_response part.")
        else:
            # Неизвестный тип part
            logger.warning(f"Encountered unknown part type in final model response: {type(part)}")

    if not extracted_texts:
        logger.info("No text found in the final model response parts.")
        return None

    full_text = "".join(extracted_texts)
    logger.info(f"Successfully extracted final text (len={len(full_text)}).")
    return full_text