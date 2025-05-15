"""
Утилиты для работы с сообщениями и их форматированием для различных API.
"""

import logging
import json
from typing import Dict, List, Any, Optional, Set

logger = logging.getLogger(__name__)

def sanitize_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Проверяет и исправляет список сообщений OpenAI перед отправкой в API, обеспечивая, что:
    1. Сообщения с ролью 'tool' идут сразу после сообщений с 'tool_calls'
    2. Каждое 'tool' сообщение имеет соответствующий tool_call_id
    3. Удаляет 'tool' сообщения, которые не удовлетворяют этим условиям

    Returns:
        List[Dict[str, Any]]: Очищенный список сообщений
    """
    if not messages:
        return []
    
    # Детальное логирование всех сообщений для диагностики
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content_preview = str(msg.get("content", ""))[:50] + "..." if len(str(msg.get("content", ""))) > 50 else str(msg.get("content", ""))
        tool_call_id = msg.get("tool_call_id", "")
        
        # Логируем основную информацию о сообщении
        log_details = f"[{i}] role={role}"
        if role == "tool":
            log_details += f", tool_call_id={tool_call_id}"
        elif role == "assistant" and "tool_calls" in msg:
            tool_calls_info = []
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict):
                    tc_id = tc.get("id", "?")
                    tc_name = tc.get("function", {}).get("name", "?") if isinstance(tc.get("function"), dict) else "?"
                    tool_calls_info.append(f"{tc_id}:{tc_name}")
            log_details += f", tool_calls=[{', '.join(tool_calls_info)}]"
        
        log_details += f", content='{content_preview}'"
        logger.debug(f"Message {log_details}")
    
    result = []
    # Отслеживаем tool_call_ids, которые упомянуты в сообщениях assistant
    available_tool_call_ids: Set[str] = set()
    # Отслеживаем уже использованные tool_call_ids в сообщениях tool
    used_tool_call_ids: Set[str] = set()
    
    # Сначала добавляем все не-tool сообщения и собираем доступные tool_call_ids
    for msg in messages:
        role = msg.get("role", "")
        
        if role == "assistant" and "tool_calls" in msg:
            for tool_call in msg.get("tool_calls", []):
                if isinstance(tool_call, dict) and "id" in tool_call:
                    available_tool_call_ids.add(tool_call["id"])
            result.append(msg)
        elif role != "tool":
            result.append(msg)
    
    logger.info(f"Collected {len(available_tool_call_ids)} valid tool_call_ids: {available_tool_call_ids}")
    
    # Теперь пытаемся добавить tool сообщения, но только те, которые имеют правильный tool_call_id
    # и идут после соответствующего assistant сообщения
    for i, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
            
        tool_call_id = msg.get("tool_call_id")
        if not tool_call_id:
            logger.warning(f"Skipping 'tool' message at position {i} without tool_call_id")
            continue
            
        if tool_call_id not in available_tool_call_ids:
            logger.warning(f"Skipping 'tool' message at position {i} with unknown tool_call_id: {tool_call_id}")
            continue
            
        if tool_call_id in used_tool_call_ids:
            logger.warning(f"Skipping duplicate 'tool' message at position {i} with tool_call_id: {tool_call_id}")
            continue
            
        # Проверяем, что перед этим tool сообщением есть assistant с соответствующим tool_call_id
        found_preceding_assistant = False
        for j, res_msg in enumerate(result):
            if res_msg.get("role") == "assistant" and "tool_calls" in res_msg:
                for tc in res_msg.get("tool_calls", []):
                    if isinstance(tc, dict) and tc.get("id") == tool_call_id:
                        found_preceding_assistant = True
                        # Вставляем tool сообщение сразу после этого assistant сообщения,
                        # а не просто добавляем в конец
                        insertion_point = j + 1
                        # Проверяем, чтобы не было других 'tool' сообщений между позициями
                        while insertion_point < len(result) and result[insertion_point].get("role") == "tool":
                            insertion_point += 1
                        result.insert(insertion_point, msg)
                        used_tool_call_ids.add(tool_call_id)
                        logger.info(f"Inserted 'tool' message with ID {tool_call_id} at position {insertion_point} (after assistant at {j})")
                        break
                if found_preceding_assistant:
                    break
        
        if not found_preceding_assistant:
            logger.warning(f"Skipping 'tool' message at position {i} as it does not follow its assistant message")
    
    # Выводим информацию о результатах очистки
    logger.info(f"OpenAI messages sanitization: {len(messages)} -> {len(result)} messages")
    if len(messages) != len(result):
        logger.warning(f"Removed {len(messages) - len(result)} problematic messages")
    
    # Для диагностики выводим финальную структуру сообщений
    tool_sequence_valid = True
    for i, msg in enumerate(result):
        if msg.get("role") == "tool":
            tool_call_id = msg.get("tool_call_id")
            
            # Проверяем, что предыдущее сообщение - assistant с tool_calls или другой tool от того же assistant
            if i > 0:
                prev_msg = result[i-1]
                if prev_msg.get("role") == "assistant" and "tool_calls" in prev_msg:
                    # Проверяем, есть ли tool_call_id в tool_calls предыдущего сообщения
                    has_matching_call = False
                    for tc in prev_msg.get("tool_calls", []):
                        if isinstance(tc, dict) and tc.get("id") == tool_call_id:
                            has_matching_call = True
                            break
                    if not has_matching_call:
                        logger.warning(f"Potential issue: tool message at position {i} with ID {tool_call_id} follows assistant at {i-1} but that assistant doesn't have matching tool_call_id")
                        tool_sequence_valid = False
                elif prev_msg.get("role") == "tool":
                    # Этот tool должен относиться к тому же assistant, что и предыдущий tool
                    continue  # Это нормально
                else:
                    logger.warning(f"Potential issue: tool message at position {i} with ID {tool_call_id} follows message with role {prev_msg.get('role')}")
                    tool_sequence_valid = False
            else:
                logger.warning(f"Potential issue: tool message at position {i} with ID {tool_call_id} has no preceding message")
                tool_sequence_valid = False
    
    logger.info(f"Final tool sequence valid: {tool_sequence_valid}")
    return result 