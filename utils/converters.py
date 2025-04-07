# utils/converters.py
import logging
import json
from typing import Dict, Any, List, Optional, Union

logger = logging.getLogger(__name__)

# --- Типы Google (только для аннотаций) ---
# Импортируем с проверкой, чтобы не падать, если пакет еще не установлен
try:
    # <<< ВОЗВРАЩАЕМ glm >>>
    from google.ai import generativelanguage as glm
    # <<< ДОБАВЛЯЕМ Импорт RepeatedComposite >>>
    from google.protobuf.internal.containers import RepeatedComposite
    Part = glm.Part
    Content = glm.Content
    FunctionResponse = glm.FunctionResponse
    FunctionCall = glm.FunctionCall
except ImportError:
    Part, Content, FunctionResponse, FunctionCall = Any, Any, Any, Any
    # <<< ДОБАВЛЯЕМ RepeatedComposite в fallback >>>
    RepeatedComposite = Any # Определяем как Any, если импорт не удался
    logging.getLogger(__name__).warning(
        "Could not import Google types (Part, Content, etc.) or RepeatedComposite. Using 'Any' for type hints."
    )



# --- Вспомогательные функции сериализации/десериализации (из v3) ---

def _serialize_parts(parts: Union[List[Dict[str, Any]], 'RepeatedComposite']) -> str:
    """Converts a list of part dicts or RepeatedComposite to a JSON string."""
    # <<< ДОБАВЛЕНО: Логгирование на входе в функцию >>>
    logger.debug(f"_serialize_parts received: type={type(parts)}, value={repr(parts)[:500]}") # Логгируем тип и начало значения
    if not isinstance(parts, (list, RepeatedComposite)): # This is where the error occurs
        logger.error(f"_serialize_parts expected a list or RepeatedComposite, got {type(parts)}. Returning empty list JSON.")
        return "[]"

    try:
        # <<< ИЗМЕНЕНИЕ: Используем parts_list >>>
        serializable_parts = [_convert_value_for_json(part) for part in parts]
        return json.dumps(serializable_parts, ensure_ascii=False)
    except TypeError as e:
        logger.error(f"Failed to serialize parts list to JSON: {e}", exc_info=True)
        # Возвращаем пустой список в случае ошибки сериализации
        return "[]"
    except Exception as e:
        logger.error(f"Unexpected error during parts serialization: {e}", exc_info=True)
        return "[]"


def _deserialize_parts(parts_json: Optional[str]) -> List[Dict[str, Any]]:
    """
    Десериализует JSON строку в список словарей.
    Возвращает пустой список при ошибке или если на входе None/пустая строка.
    """
    if not parts_json:
        return []
    try:
        data = json.loads(parts_json)
        if isinstance(data, list):
            # Дополнительно проверяем, что элементы списка - словари (опционально)
            if all(isinstance(item, dict) for item in data):
                return data
            else:
                logger.warning(f"_deserialize_parts: JSON list contains non-dict elements. Returning as is.")
                return data # Возвращаем как есть, но логируем
        else:
             logger.error(f"_deserialize_parts: JSON string did not decode into a list. Got type: {type(data)}")
             return [] # Возвращаем пустой список, если это не список
    except json.JSONDecodeError as e:
        logger.error(f"Failed to deserialize parts JSON: {e}. JSON: '{parts_json[:100]}...'")
        return [{"error": "deserialization_failed", "original_json": parts_json[:100] + "..."}]
    except Exception as e:
        logger.error(f"Unexpected error during parts deserialization: {e}", exc_info=True)
        return []



def _is_map_composite(obj: Any) -> bool:
    """Проверяет, похож ли объект на MapComposite (утиная типизация)."""
    return hasattr(obj, 'keys') and hasattr(obj, 'values') and hasattr(obj, 'items') and \
           not isinstance(obj, dict)

def _convert_value_for_json(value: Any) -> Any:
    """
    Рекурсивно конвертирует вложенные структуры (включая объекты Google)
    в типы, совместимые с JSON-сериализацией (dict, list, str, int, float, bool, None).
    """
    if isinstance(value, dict):
        # Конвертируем ключи в строки и рекурсивно обрабатываем значения
        return {str(k): _convert_value_for_json(v) for k, v in value.items()}
    elif isinstance(value, list):
        # Рекурсивно обрабатываем элементы списка
        return [_convert_value_for_json(item) for item in value]
    elif _is_map_composite(value):
         # Обрабатываем MapComposite-подобные объекты как словари
         logger.debug(f"Converting MapComposite-like object to dict: {type(value)}")
         return {str(k): _convert_value_for_json(v) for k, v in value.items()}
    # Обработка объектов с методом to_dict (например, объекты Google)
    elif hasattr(value, 'to_dict') and callable(value.to_dict):
        try:
            dict_repr = value.to_dict()
            # Рекурсивно обрабатываем результат to_dict
            return _convert_value_for_json(dict_repr)
        except Exception as e:
            logger.warning(f"Calling to_dict() failed for {type(value)}: {e}. Converting to string.")
            return str(value)
    # Базовые типы, совместимые с JSON
    elif isinstance(value, (str, int, float, bool, type(None))):
        return value
    # Для всех остальных неподдерживаемых типов
    else:
        logger.warning(f"Cannot directly serialize type {type(value)}. Converting to string.")
        return str(value)

def _convert_part_to_dict(part: Part) -> Optional[Dict[str, Any]]:
    """
    Преобразует объект google.ai.generativelanguage.Part в словарь Python.
    Корректно обрабатывает наличие text, function_call и function_response.
    Игнорирует FC/FR с невалидными (пустыми) именами.
    Возвращает словарь, если есть хотя бы одно валидное поле (text, fc, fr).
    """
    part_dict = {}
    has_valid_content = False

    try:
        # --- Текст ---
        # Сохраняем текст, даже если он пустой, если это ЕДИНСТВЕННОЕ содержимое.
        # Но если есть FC или FR, пустой текст можно проигнорировать.
        part_text = getattr(part, 'text', None)
        if isinstance(part_text, str): # Проверяем, что атрибут text есть и это строка
             part_dict['text'] = part_text # Сохраняем текст (может быть пустым)
             if part_text: # Считаем валидным контентом только непустой текст
                 has_valid_content = True

        # --- FunctionCall ---
        fc = getattr(part, 'function_call', None)
        if fc is not None:
            fc_name = getattr(fc, 'name', None)
            if isinstance(fc_name, str) and fc_name.strip():
                # --- Код обработки fc_args (остается как был) ---
                fc_args_converted = {"error": "conversion failed"}
                fc_args_raw = getattr(fc, 'args', None)
                if fc_args_raw is not None:
                    try:
                        fc_args_converted = _convert_value_for_json(fc_args_raw)
                        if not isinstance(fc_args_converted, dict):
                            logger.error(f"Conversion of function_call args did not result in a dict for '{fc_name}'. Args type: {type(fc_args_converted)}")
                            fc_args_converted = {"error": "failed to parse args structure"}
                    except Exception as e:
                        logger.error(f"Could not convert function_call args to dict for '{fc_name}': {e}. Raw Args: {fc_args_raw}")
                        fc_args_converted = {"error": f"failed to parse args: {e}"}
                else:
                    fc_args_converted = {}
                # --- Конец кода обработки fc_args ---
                part_dict['function_call'] = {'name': fc_name, 'args': fc_args_converted}
                has_valid_content = True
            else:
                logger.debug(f"Ignoring invalid function_call name: '{fc_name}' during conversion.")

        # --- FunctionResponse ---
        fr = getattr(part, 'function_response', None)
        if fr is not None:
            fr_name = getattr(fr, 'name', None)
            if isinstance(fr_name, str) and fr_name.strip():
                # --- Код обработки fr_response (остается как был) ---
                fr_response_converted = {"error": "conversion failed"}
                fr_response_raw = getattr(fr, 'response', None)
                if fr_response_raw is not None:
                    try:
                        fr_response_converted = _convert_value_for_json(fr_response_raw)
                        if not isinstance(fr_response_converted, dict):
                            logger.error(f"Conversion of function_response 'response' did not result in a dict for '{fr_name}'. Type: {type(fr_response_converted)}")
                            fr_response_converted = {"error": "failed to parse response structure"}
                    except Exception as e:
                        logger.error(f"Could not convert function_response 'response' for '{fr_name}': {e}. Raw Response: {fr_response_raw}")
                        fr_response_converted = {"error": f"failed to parse response: {e}"}
                else:
                    fr_response_converted = {}
                # --- Конец кода обработки fr_response ---
                part_dict['function_response'] = {'name': fr_name, 'response': fr_response_converted}
                has_valid_content = True
            else:
                 logger.debug(f"Ignoring invalid function_response name: '{fr_name}' during conversion.")


        # <<< УТОЧНЕННАЯ ЛОГИКА ВОЗВРАТА >>>
        # Если есть валидный FC или FR, или НЕПУСТОЙ текст, возвращаем словарь.
        # Если есть ТОЛЬКО ПУСТОЙ текст, то НЕ возвращаем словарь (None),
        # чтобы не создавать пустые записи в истории без FC/FR.
        if has_valid_content:
             # Если есть FC/FR, удаляем поле 'text', если оно пустое
             if ('function_call' in part_dict or 'function_response' in part_dict) and 'text' in part_dict and not part_dict['text']:
                  del part_dict['text']
             return part_dict
        elif 'text' in part_dict and not part_dict['text']: # Если был только пустой текст
             logger.debug("Part contained only empty text after processing. Returning None.")
             return None
        else: # Не было ни текста, ни валидного FC/FR
             return None

    except Exception as e:
        logger.error(f"Error converting Part to dict: {e}. Part: {part}", exc_info=True)
        return None

def gemini_history_to_dict_list(history: Optional[List[Content]]) -> List[Dict[str, Any]]:
    """
    Преобразует историю Gemini (список объектов Content) в список словарей Python.

    Args:
        history: Список объектов google.ai.generativelanguage.Content или None.

    Returns:
        Список словарей, где каждый словарь представляет запись истории
        с ключами 'role' и 'parts' (список словарей).
    """
    dict_list: List[Dict[str, Any]] = []
    if not history:
        return dict_list

    for entry in history:
        if not isinstance(entry, Content):
            logger.warning(f"Skipping non-Content item in history: {type(entry)}")
            continue

        role = getattr(entry, 'role', None)
        if not role:
            logger.warning("History entry missing role, skipping.")
            continue

        parts_list_of_dicts: List[Dict[str, Any]] = []
        if hasattr(entry, 'parts') and isinstance(entry.parts, (list, tuple)):
            for p in entry.parts:
                # Конвертируем каждую часть и добавляем, если результат не None
                converted_part = _convert_part_to_dict(p)
                if converted_part is not None:
                    parts_list_of_dicts.append(converted_part)
                else:
                    # Логируем, что часть была пропущена (возможно, ошибка конвертации)
                    logger.debug(f"Part conversion returned None for role '{role}', part type: {type(p)}. Skipping part.")

        # <<< ИЗМЕНЕНИЕ: УДАЛЯЕМ проверку 'if parts_list_of_dicts:' >>>
        # Добавляем запись, если есть роль, даже если parts_list_of_dicts пуст
        # Это сохранит структуру диалога, даже если содержимое ответа модели было проблемным
        # if parts_list_of_dicts: # <-- Удаляем эту строку
        #     dict_list.append({"role": role, "parts": parts_list_of_dicts})
        # elif role: # <-- Удаляем эту строку
        #      logger.debug(f"History entry for role '{role}' resulted in empty parts list after conversion. Skipping entry.") # <-- Удаляем эту строку

        # <<< ИЗМЕНЕНИЕ: Добавляем всегда, если есть роль >>>
        dict_list.append({"role": role, "parts": parts_list_of_dicts})
        if not parts_list_of_dicts:
            logger.debug(f"History entry for role '{role}' resulted in empty parts list after conversion, but entry structure is saved.")

    return dict_list

# <<< НАЧАЛО НОВОЙ ФУНКЦИИ >>>
def reconstruct_content_object(role: str, parts_list_of_dicts: List[Dict[str, Any]]) -> Optional[Content]:
    """
    Воссоздает объект google.ai.generativelanguage.Content из роли и списка словарей,
    представляющих его части (включая text, function_call, function_response).

    Args:
        role: Роль ('user' или 'model').
        parts_list_of_dicts: Список словарей, десериализованный из parts_json.

    Returns:
        Объект google.ai.generativelanguage.Content или None, если возникла ошибка.
    """
    try:
        reconstructed_parts: List[Part] = []
        for part_dict in parts_list_of_dicts:
            if not isinstance(part_dict, dict):
                logger.warning(f"Skipping non-dict item in parts_list_of_dicts: {part_dict}")
                continue

            # Создаем объект Part
            new_part = glm.Part()
            part_has_content = False

            # Восстанавливаем текст
            if 'text' in part_dict:
                text_content = part_dict['text']
                if isinstance(text_content, str):
                    new_part.text = text_content
                    part_has_content = True
                else:
                    logger.warning(f"Reconstruct: Invalid type for text content: type={type(text_content)}, value='{str(text_content)[:50]}...'. Skipping part.")

            # Восстанавливаем FunctionCall
            if 'function_call' in part_dict and isinstance(part_dict['function_call'], dict):
                fc_data = part_dict['function_call']
                fc_name = fc_data.get('name')
                fc_args = fc_data.get('args', {}) # Args должны быть словарем
                if isinstance(fc_name, str) and fc_name.strip() and isinstance(fc_args, dict):
                    try:
                         # Пытаемся создать FunctionCall. Аргументы передаем как есть (словарь).
                         # <<< ИЗМЕНЕНИЕ: Используем glm.FunctionCall >>>
                         new_part.function_call = glm.FunctionCall(name=fc_name, args=fc_args)
                         part_has_content = True
                    except Exception as fc_err:
                         logger.error(f"Failed to reconstruct FunctionCall for '{fc_name}': {fc_err}. Data: {fc_data}", exc_info=True)
                else:
                     logger.warning(f"Skipping invalid function_call data during reconstruction: Name='{fc_name}', Args Type='{type(fc_args)}'")


            # Восстанавливаем FunctionResponse
            if 'function_response' in part_dict and isinstance(part_dict['function_response'], dict):
                fr_data = part_dict['function_response']
                fr_name = fr_data.get('name')
                # Response может быть любым JSON-совместимым типом, но ожидается словарь
                fr_response = fr_data.get('response', {})
                if isinstance(fr_name, str) and fr_name.strip():
                     try:
                         # Пытаемся создать FunctionResponse. Response передаем как есть.
                         # <<< ИЗМЕНЕНИЕ: Используем glm.FunctionResponse >>>
                         new_part.function_response = glm.FunctionResponse(name=fr_name, response=fr_response)
                         part_has_content = True
                     except Exception as fr_err:
                         logger.error(f"Failed to reconstruct FunctionResponse for '{fr_name}': {fr_err}. Data: {fr_data}", exc_info=True)
                else:
                    logger.warning(f"Skipping invalid function_response data during reconstruction: Name='{fr_name}'")


            # Добавляем созданную часть в список, если она не пустая
            if part_has_content:
                reconstructed_parts.append(new_part)
            else:
                logger.warning(f"Skipping part reconstruction as it resulted in empty content: {part_dict}")


        # Если после обработки всех частей список не пуст, создаем Content
        if reconstructed_parts:
            # <<< ИЗМЕНЕНИЕ: Используем glm.Content >>>
            return glm.Content(role=role, parts=reconstructed_parts)
        else:
            logger.warning(f"Reconstruction resulted in no valid parts for role '{role}'. Original data: {parts_list_of_dicts}")
            return None

    except Exception as e:
        logger.error(f"Failed to reconstruct Content object for role '{role}': {e}", exc_info=True)
        return None
# <<< КОНЕЦ НОВОЙ ФУНКЦИИ >>>