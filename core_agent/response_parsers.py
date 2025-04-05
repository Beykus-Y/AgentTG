# core_agent/response_parsers.py
import json
import logging
import re
from typing import List, Dict, Tuple, Optional, Any, Union

logger = logging.getLogger(__name__)

LiteParseResult = Union[List[Dict[str, Any]], Dict[str, Any], str]

def parse_lite_llm_response(text_response: Optional[str]) -> LiteParseResult:
    """
    Парсит, очищает и валидирует JSON-ответ от Lite LLM.
    Возвращает список действий, "NO_ACTION_NEEDED" или словарь ошибки.
    """
    if not text_response:
        logger.info("Lite LLM returned empty text response. Interpreting as NO_ACTION_NEEDED.")
        return "NO_ACTION_NEEDED"

    cleaned_text = text_response.strip()
    original_cleaned_text = cleaned_text
    is_cleaned = False
    logger.debug(f"Parsing Lite LLM response. Initial text: '{text_response[:100]}...'")

    if cleaned_text.startswith("```") and cleaned_text.endswith("```"):
        cleaned_text = cleaned_text[3:-3].strip()
        is_cleaned = True
        first_line_end = cleaned_text.find('\n')
        if first_line_end != -1:
            first_line = cleaned_text[:first_line_end].strip()
            if first_line.lower() == "json":
                 cleaned_text = cleaned_text[first_line_end:].strip()
        elif cleaned_text.lower().startswith("json"):
             cleaned_text = cleaned_text[4:].strip()

    elif not (cleaned_text.startswith("{") and cleaned_text.endswith("}")):
        logger.warning(f"Lite LLM response does not look like JSON or Markdown block. Raw: '{text_response[:100]}...'")

    if is_cleaned:
        logger.debug(f"Markdown cleaned. Text for parsing: '{cleaned_text[:100]}...'")

    try:
        parsed_json = json.loads(cleaned_text)

        if isinstance(parsed_json, dict) and \
           "actions_to_perform" in parsed_json and \
           isinstance(parsed_json.get("actions_to_perform"), list):

            actions = parsed_json["actions_to_perform"]

            if not actions:
                logger.info("Lite LLM response parsed: No actions needed.")
                return "NO_ACTION_NEEDED"
            else:
                logger.info(f"Lite LLM response parsed: Actions requested: {actions}")
                valid_actions = []
                for action in actions:
                    if isinstance(action, dict) and \
                       "function_name" in action and \
                       "arguments" in action and \
                       isinstance(action.get("arguments"), dict):

                        args = action["arguments"]
                        try:
                            # Конвертация типов
                            if 'user_id' in args and args['user_id'] is not None and not isinstance(args['user_id'], int):
                                args['user_id'] = int(float(args['user_id']))
                            if 'chat_id' in args and args['chat_id'] is not None and not isinstance(args['chat_id'], int):
                                args['chat_id'] = int(float(args['chat_id']))
                            # Добавьте другие конвертации
                        except (ValueError, TypeError, KeyError) as conv_err:
                            logger.warning(f"Argument conversion/access failed for action {action.get('function_name')}: {conv_err}. Skipping action. Args: {args}")
                            continue

                        valid_actions.append({"function_name": action["function_name"], "arguments": args})
                    else:
                         logger.warning(f"Invalid action structure in JSON: {action}")

                if not valid_actions and actions:
                     error_message = "Actions list contains only invalid action structures."
                     logger.error(f"Lite LLM Error: {error_message}. Original actions: {actions}")
                     return {"error": "INVALID_ACTION_STRUCTURE", "message": error_message, "details": actions}

                return valid_actions
        else:
            error_message = "Invalid JSON structure received from Lite LLM (missing 'actions_to_perform' list)."
            logger.error(f"{error_message} Parsed from: {cleaned_text}")
            return {"error": "INVALID_JSON_STRUCTURE", "message": error_message, "details": cleaned_text}

    except json.JSONDecodeError:
        error_message = "Failed to decode JSON response from Lite LLM (Model did not return valid JSON)."
        logger.error(error_message)
        logger.error(f"Original Response text: '{text_response[:100]}...'")
        logger.error(f"Cleaned text for parsing: '{original_cleaned_text[:100]}...'")
        return {"error": "JSON_DECODE_ERROR", "message": error_message, "original_details": text_response, "cleaned_details": original_cleaned_text}
    except Exception as e:
         error_message = f"Unexpected error parsing Lite LLM response: {e}"
         logger.error(error_message, exc_info=True)
         return {"error": "UNEXPECTED_PARSING_ERROR", "message": error_message, "details": cleaned_text}