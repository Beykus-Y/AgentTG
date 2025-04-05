# tools/environment_tools.py

import logging
import asyncio
import os
import re # Добавляем импорт re
import ast # Для AST трансформации
import json # Для редактирования JSON
from typing import Dict, Optional, List, Tuple, Any
import secrets
from aiogram.types import FSInputFile
from jsonpath_ng.ext import parse

# --- Локальные зависимости ---
try:
    # Менеджер окружения для безопасных путей
    from services.env_manager import get_safe_chat_path, _ensure_specific_chat_dir_exists as ensure_chat_dir
    # Трансформер AST
    from ._ast_transformer import ReplaceCodeTransformer
    # Настройки для лимитов
    from config import settings, Settings
except ImportError as e:
    logging.critical(f"CRITICAL: Failed to import dependencies (env_manager, _ast_transformer, config) in environment_tools.", exc_info=True)
    logging.warning("Using Mock functions/classes for env_manager, AST transformer, and settings in environment_tools.")
    # Заглушки
    async def get_safe_chat_path(*args, **kwargs): return False, None
    async def ensure_chat_dir(*args, **kwargs): return False
    class ReplaceCodeTransformer: pass
    class MockSettings:
        max_read_size_bytes: int = 100 * 1024
        max_write_size_bytes: int = 500 * 1024
        script_timeout_seconds: int = 30
        command_timeout_seconds: int = 60
        max_script_output_len: int = 5000
        max_command_output_len: int = 5000
    settings = MockSettings()

# Асинхронные файловые операции
try:
    import aiofiles
    import aiofiles.os
except ImportError:
    aiofiles = None # type: ignore
    logging.critical("CRITICAL: 'aiofiles' library not found. File operations will fail.")

logger = logging.getLogger(__name__)

# --- Файловые операции (Async) ---

async def read_file_from_env(user_id: int, chat_id: int, filename: str) -> Dict[str, Any]:
    """
    Читает содержимое файла из окружения чата асинхронно.
    Проверяет размер файла перед чтением.
    Возвращает словарь со статусом, сообщением и содержимым файла.

    Args:
        user_id (int): ID пользователя.
        chat_id (int): ID текущего чата.
        filename (str): Относительный путь к файлу.

    Returns:
        dict: Словарь со статусом операции, сообщением и контентом (или None при ошибке).
              Пример: {'status': 'success', 'message': 'File read.', 'content': '...', 'filename': '...'}
    """
    tool_name = "read_file_from_env"
    logger.info(f"--- Tool Call: {tool_name}(user={user_id}, chat={chat_id}, file='{filename}') ---")

    if aiofiles is None:
        return {"status": "error", "message": "Internal error: aiofiles library missing.", "content": None}

    # Проверяем путь БЕЗ создания директории чата
    is_safe, filepath = await get_safe_chat_path(chat_id, filename, user_id=user_id, ensure_chat_dir_exists=False)
    if not is_safe or filepath is None:
        return {"status": "error", "message": "Access denied or invalid filename.", "content": None}

    try:
        # Проверяем существование и тип файла асинхронно
        if not await aiofiles.os.path.exists(filepath):
            return {"status": "error", "message": f"File '{filename}' not found.", "content": None}
        if await aiofiles.os.path.isdir(filepath):
             return {"status": "error", "message": f"'{filename}' is a directory, not a file.", "content": None}

        # Читаем файл асинхронно
        async with aiofiles.open(filepath, mode="r", encoding='utf-8', errors='ignore') as f:
            content = await f.read()

        logger.info(f"{tool_name}: Successfully read file '{filename}' (size: {len(content)} bytes).")
        # Возвращаем успех с контентом
        return {
            "status": "success",
            "message": f"File '{filename}' read successfully (size: {len(content)} bytes).",
            "content": content,
            "filename": filename, # Возвращаем имя файла для контекста
        }

    except FileNotFoundError: # Должно быть поймано exists(), но оставим для надежности
        logger.warning(f"{tool_name}: File '{filename}' not found unexpectedly at path '{filepath}'.")
        return {"status": "error", "message": f"File '{filename}' not found.", "content": None}
    except Exception as e:
        msg = f"Error reading file '{filename}': {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg, "content": None}


async def write_file_to_env(user_id: int, chat_id: int, filename: str, content: str) -> Dict[str, str]:
    """
    Записывает (или перезаписывает) текст в файл в окружении чата.
    Администраторы могут писать в директории других чатов внутри /env.
    Использует aiofiles для асинхронной записи.

    Args:
        user_id (int): ID пользователя.
        chat_id (int): ID текущего чата.
        filename (str): Относительный путь к файлу.
        content (str): Содержимое для записи.

    Returns:
        dict: Словарь со статусом операции.
    """
    tool_name = "write_file_to_env"
    logger.info(f"--- Tool Call: {tool_name}(user={user_id}, chat={chat_id}, file='{filename}', content_len={len(content)}) ---")

    if aiofiles is None: return {"status": "error", "message": "Internal error: aiofiles library missing."}

    # Проверяем путь и УБЕЖДАЕМСЯ, что базовая директория чата существует
    is_safe, filepath = await get_safe_chat_path(chat_id, filename, user_id=user_id, ensure_chat_dir_exists=True)
    if not is_safe or filepath is None:
        return {"status": "error", "message": "Access denied, invalid filename, or failed to ensure base chat directory."}

    # Проверка размера контента
    try:
        content_bytes = content.encode('utf-8')
        # ----- УДАЛЕНО: Проверка максимального размера записи -----
        # if len(content_bytes) > settings.max_write_size_bytes:
        #     msg = f"Error: Content size ({len(content_bytes)} bytes) exceeds limit ({settings.max_write_size_bytes // 1024} KB)."
        #     logger.error(f"{tool_name}: {msg} for file '{filename}'")
        #     return {"status": "error", "message": msg}
        # ----------------------------------------------------------
    except Exception as e:
        logger.error(f"{tool_name}: Error encoding content for size check: {e}")
        return {"status": "error", "message": "Error checking content size."}


    try:
        # Асинхронно создаем родительские директории для САМОГО ФАЙЛА
        parent_dir = os.path.dirname(filepath)
        if parent_dir:
             await aiofiles.os.makedirs(parent_dir, exist_ok=True)
             logger.debug(f"{tool_name}: Ensured parent directory exists: {parent_dir}")

        # Асинхронная запись
        async with aiofiles.open(filepath, mode="w", encoding='utf-8') as f:
            await f.write(content)

        logger.info(f"{tool_name}: Successfully wrote file '{filename}' (size: {len(content_bytes)} bytes).")
        return {"status": "success", "message": f"File '{filename}' written successfully."}
    except Exception as e:
        msg = f"Error writing file '{filename}': {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg}


async def create_file_in_env(user_id: int, chat_id: int, filename: str) -> Dict[str, str]:
    """
    Создает новый пустой файл в окружении чата.
    Использует aiofiles для асинхронных операций.

    Args:
        user_id (int): ID пользователя.
        chat_id (int): ID текущего чата.
        filename (str): Относительный путь к файлу.

    Returns:
        dict: Словарь со статусом операции.
    """
    tool_name = "create_file_in_env"
    logger.info(f"--- Tool Call: {tool_name}(user={user_id}, chat={chat_id}, file='{filename}') ---")

    if aiofiles is None: return {"status": "error", "message": "Internal error: aiofiles library missing."}

    # Проверяем путь и УБЕЖДАЕМСЯ, что базовая директория чата существует
    is_safe, filepath = await get_safe_chat_path(chat_id, filename, user_id=user_id, ensure_chat_dir_exists=True)
    if not is_safe or filepath is None:
          return {"status": "error", "message": "Access denied, invalid filename, or failed to ensure base chat directory."}

    try:
        # Асинхронно проверяем, не существует ли уже файл/директория
        if await aiofiles.os.path.exists(filepath):
               is_dir = await aiofiles.os.path.isdir(filepath)
               entity_type = "directory" if is_dir else "file"
               msg = f"Error: Cannot create. {entity_type.capitalize()} '{filename}' already exists."
               logger.warning(f"{tool_name}: {msg}")
               return {"status": "error", "message": msg}

        # Асинхронно создаем родительские директории для САМОГО ФАЙЛА
        parent_dir = os.path.dirname(filepath)
        if parent_dir:
            await aiofiles.os.makedirs(parent_dir, exist_ok=True)
            logger.debug(f"{tool_name}: Ensured parent directory exists: {parent_dir}")

        # Создаем пустой файл асинхронно
        async with aiofiles.open(filepath, mode="w", encoding='utf-8') as f:
               await f.write("") # Пишем пустую строку

        logger.info(f"{tool_name}: Successfully created empty file '{filename}'")
        return {"status": "success", "message": f"File '{filename}' created successfully."}
    except Exception as e:
        msg = f"Error creating file '{filename}': {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg}


# --- Выполнение скриптов и команд (используют asyncio.create_subprocess) ---

async def _run_subprocess(cmd_list: list[str], cwd: str, timeout: int) -> Tuple[int, str, str]:
    """Вспомогательная асинхронная функция для запуска subprocess."""
    process = None
    # ----- ИЗМЕНЕНО: Увеличено значение таймаута -----
    effective_timeout = 3600 # Например, 1 час
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd_list,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            # ----- УДАЛЕНО: Лимит буфера -----
            # limit=settings.max_script_output_len * 2 # Устанавливаем лимит на размер буфера stdout/stderr
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=effective_timeout)
        returncode = process.returncode if process.returncode is not None else -1 # Возвращаем -1, если процесс еще не завершился (маловероятно)

        # Декодируем с игнорированием ошибок
        # ----- УДАЛЕНО: Обрезка stdout/stderr -----
        stdout = stdout_bytes.decode('utf-8', errors='ignore') #[:settings.max_script_output_len]
        stderr = stderr_bytes.decode('utf-8', errors='ignore') #[:settings.max_script_output_len]

        # if len(stdout_bytes) > settings.max_script_output_len: stdout += "...[truncated]"
        # if len(stderr_bytes) > settings.max_script_output_len: stderr += "...[truncated]"
        # --------------------------------------------

        return returncode, stdout, stderr
    except asyncio.TimeoutError:
        # ----- ИЗМЕНЕНО: Сообщение об ошибке таймаута -----
        timeout_msg = f"Error: Process timed out after {effective_timeout} seconds."
        logger.warning(f"{timeout_msg} Cmd: {cmd_list}")
        if process and process.returncode is None: # Если процесс еще жив
             try: process.kill()
             except ProcessLookupError: pass # Процесс мог завершиться сам
             await process.wait() # Дожидаемся завершения после kill
        return -99, "", timeout_msg # Специальный код для таймаута
    except Exception as e:
         logger.error(f"Error during subprocess execution: {e}. Cmd: {cmd_list}", exc_info=True)
         return -100, "", f"Error during subprocess execution: {e}" # Другая ошибка


async def _run_subprocess_shell(command: str, cwd: str, timeout: int) -> Tuple[int, str, str]:
    """Вспомогательная асинхронная функция для запуска subprocess с shell=True."""
    process = None
    # ----- ИЗМЕНЕНО: Увеличено значение таймаута -----
    effective_timeout = 3600 # Например, 1 час
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            # ----- УДАЛЕНО: Лимит буфера -----
            # limit=settings.max_command_output_len * 2 # Лимит буфера для shell
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=effective_timeout)
        returncode = process.returncode if process.returncode is not None else -1

        # Декодируем и обрезаем
        # ----- УДАЛЕНО: Обрезка stdout/stderr -----
        stdout = stdout_bytes.decode('utf-8', errors='ignore') #[:settings.max_command_output_len]
        stderr = stderr_bytes.decode('utf-8', errors='ignore') #[:settings.max_command_output_len]

        # if len(stdout_bytes) > settings.max_command_output_len: stdout += "...[truncated]"
        # if len(stderr_bytes) > settings.max_command_output_len: stderr += "...[truncated]"
        # --------------------------------------------

        return returncode, stdout, stderr
    except asyncio.TimeoutError:
        # ----- ИЗМЕНЕНО: Сообщение об ошибке таймаута -----
        timeout_msg = f"Error: Command shell timed out after {effective_timeout} seconds."
        logger.warning(f"{timeout_msg} Command: {command}")
        if process and process.returncode is None:
            try: process.kill()
            except ProcessLookupError: pass
            await process.wait()
        return -99, "", timeout_msg # Специальный код для таймаута
    except Exception as e:
         logger.error(f"Error during shell command execution: {e}. Cmd: {command[:100]}...", exc_info=True)
         return -100, "", f"Error during shell command execution: {e}"


async def execute_python_script_in_env(user_id: int, chat_id: int, filename: str) -> Dict[str, Any]:
    """
    Выполняет Python-скрипт из окружения чата асинхронно.
    Использует asyncio.create_subprocess_exec.

    Args:
        user_id (int): ID пользователя.
        chat_id (int): ID текущего чата.
        filename (str): Относительный путь к Python-скрипту (.py).

    Returns:
        dict: Словарь с stdout, stderr, returncode и статусом.
    """
    tool_name = "execute_python_script_in_env"
    logger.info(f"--- Tool Call: {tool_name}(user={user_id}, chat={chat_id}, file='{filename}') ---")

    if aiofiles is None: return {"status": "error", "message": "Internal error: aiofiles library missing.", "returncode": -1}

    # Проверяем путь и существование ДИРЕКТОРИИ чата
    is_safe, filepath = await get_safe_chat_path(chat_id, filename, user_id=user_id, ensure_chat_dir_exists=True)
    if not is_safe or filepath is None:
        return {"status": "error", "message": "Access denied, invalid filename, or failed to ensure base chat directory.", "returncode": -1}

    # Определяем рабочую директорию (директория, где лежит скрипт)
    script_dir = os.path.dirname(filepath)
    script_basename = os.path.basename(filepath) # Только имя файла

    # Дополнительные проверки файла асинхронно
    try:
        if not await aiofiles.os.path.exists(filepath):
            return {"status": "error", "message": f"Error: Script file '{filename}' not found.", "returncode": -1}
        if not filename.lower().endswith(".py"):
            return {"status": "error", "message": "Error: Only Python scripts (.py) can be executed.", "returncode": -1}
        if await aiofiles.os.path.isdir(filepath):
             return {"status": "error", "message": f"Error: '{filename}' is a directory, not a script.", "returncode": -1}
    except Exception as e:
         logger.error(f"Error checking script file '{filepath}': {e}", exc_info=True)
         return {"status": "error", "message": f"Error checking script file: {e}", "returncode": -1}


    try:
        # Запускаем python и передаем ему имя скрипта как аргумент
        # Рабочая директория (cwd) = директория скрипта
        logger.info(f"Executing Python script '{script_basename}' in '{script_dir}'...")
        returncode, stdout, stderr = await _run_subprocess(
            ["python", script_basename], # Команда и аргумент
            cwd=script_dir,
            timeout=settings.script_timeout_seconds
        )

        status = "success" if returncode == 0 else "error"
        message = f"Script executed {'successfully' if returncode == 0 else 'with errors'}."
        if returncode == -99: # Таймаут
            status = "error"
            message = stderr # Сообщение об ошибке таймаута
        elif returncode == -100: # Python не найден (спец. код не нужен, ловится FileNotFoundError)
             pass # Обрабатывается ниже
        elif returncode != 0: # Другая ошибка выполнения скрипта
            message += f" Exit code: {returncode}."

        logger.info(f"{tool_name}: Script '{filename}' executed. Exit code: {returncode}")
        return {
            "status": status,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "message": message
        }

    except FileNotFoundError:
         # Эта ошибка возникнет, если 'python' не найден в PATH
         logger.error(f"{tool_name}: 'python' command not found in system PATH.")
         return {"status": "error", "message": "Error: Python interpreter not found.", "stdout": "", "stderr": "", "returncode": -101}
    except Exception as e:
        # Другие ошибки на уровне запуска процесса
        msg = f"Error preparing to execute script '{filename}': {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg, "stdout": "", "stderr": "", "returncode": -100}


async def execute_terminal_command_in_env(user_id: int, chat_id: int, command: str) -> Dict[str, Any]:
    """
    Выполняет команду в терминале в рабочей директории окружения чата асинхронно.
    Требует подтверждения AI! Использует asyncio.create_subprocess_shell.

    Args:
        user_id (int): ID пользователя.
        chat_id (int): ID текущего чата.
        command (str): Команда терминала.

    Returns:
        dict: Словарь с stdout, stderr, returncode и статусом.
    """
    tool_name = "execute_terminal_command_in_env"
    logger.info(f"--- Tool Call: {tool_name}(user={user_id}, chat={chat_id}, cmd='{command[:100]}...') ---")

    # Получаем безопасный путь к ДИРЕКТОРИИ чата, убеждаемся, что она существует
    # Передаем "." как filename, чтобы get_safe_chat_path вернул путь к директории чата
    is_safe, safe_path_result = await get_safe_chat_path(
        chat_id,
        ".", # Запрашиваем путь к самой директории
        user_id=user_id,
        ensure_chat_dir_exists=True # Убеждаемся, что директория создана
    )

    if not is_safe or safe_path_result is None:
        # get_safe_chat_path уже логирует ошибку
        return {"status": "error", "message": "Access denied, invalid chat directory, or failed to ensure directory existence.", "returncode": -1}

    # --- ИСПРАВЛЕНО: Определяем CWD правильно ---
    chat_dir_abs = safe_path_result # safe_path_result УЖЕ является путем к директории чата
    try:
        # Дополнительная проверка, что это действительно директория
        if not await aiofiles.os.path.isdir(chat_dir_abs):
             logger.error(f"Path '{chat_dir_abs}' returned by get_safe_chat_path is not a directory. Cannot determine CWD for chat {chat_id}.")
             return {"status": "error", "message": "Internal error: Determined path is not a directory.", "returncode": -1}

        # Проверка, что путь не корневой env (на всякий случай, хотя get_safe_chat_path должен это гарантировать)
        # Импортируем настройки здесь, чтобы избежать потенциальных циклических зависимостей на уровне модуля
        from config import settings
        base_env_dir = os.path.abspath(settings.env_dir_path)
        if chat_dir_abs == base_env_dir:
             logger.error(f"Determined CWD is the root env directory '{chat_dir_abs}'. This should not happen for chat execution. Chat ID: {chat_id}")
             return {"status": "error", "message": "Internal security error: Cannot execute in root env directory.", "returncode": -1}

        logger.debug(f"Determined CWD for command execution: {chat_dir_abs}")
    except ImportError:
        logger.error("Failed to import config settings within execute_terminal_command_in_env for CWD check.")
        return {"status": "error", "message": "Internal configuration error during CWD check.", "returncode": -1}
    except Exception as path_err:
         logger.error(f"Error verifying CWD path '{chat_dir_abs}': {path_err}", exc_info=True)
         return {"status": "error", "message": f"Internal error verifying execution directory: {path_err}", "returncode": -1}
    # --- КОНЕЦ ИСПРАВЛЕНИЯ CWD ---

    # Запускаем команду в shell
    logger.info(f"{tool_name}: Executing command in shell: '{command}' in '{chat_dir_abs}'")
    try:
        # Используем полученный chat_dir_abs как cwd
        returncode, stdout, stderr = await _run_subprocess_shell(
            command,
            cwd=chat_dir_abs, # Убеждаемся, что используем исправленный chat_dir_abs
            timeout=settings.command_timeout_seconds
        )

        # Логика обработки результата остается без изменений
        status = "success" if returncode == 0 else "error"
        message = f"Command shell executed {'successfully' if returncode == 0 else 'with errors'}."
        if returncode == -99: # Таймаут
            status = "timeout" # Используем статус timeout
            message = stderr # Сообщение об ошибке таймаута
        elif returncode == -100: # Ошибка запуска
             message = stderr # Сообщение об ошибке
        elif returncode != 0: # Другая ошибка выполнения
            message += f" Exit code: {returncode}."

        logger.info(f"{tool_name}: Command '{command[:50]}...' executed. Exit code: {returncode}")
        return {
            "status": status,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "message": message,
            "command": command # Возвращаем команду для контекста
        }
    # Обработка ошибок остается
    except Exception as e:
        msg = f"Failed to execute command '{command}'. Error: {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg, "command": command, "return_code": -1, "stdout": "", "stderr": ""}


# --- Редактирование файлов (AST и JSON) ---

async def edit_file_content(
    user_id: int, chat_id: int, filename: str, search_string: str, replace_string: str
) -> Dict[str, str]:
    """
    Редактирует файл в окружении чата, заменяя текст. Использует aiofiles.

    Args:
        user_id: ID пользователя.
        chat_id: ID текущего чата.
        filename: Относительный путь к файлу.
        search_string: Строка для поиска.
        replace_string: Строка для замены.

    Returns:
        dict: Словарь со статусом операции.
    """
    tool_name = "edit_file_content"
    logger.info(f"--- Tool Call: {tool_name}(user={user_id}, chat={chat_id}, file='{filename}', ... ) ---")

    if aiofiles is None: return {"status": "error", "message": "Internal error: aiofiles library missing."}

    # Проверяем путь (не создаем директорию чата заранее)
    is_safe, filepath = await get_safe_chat_path(chat_id, filename, user_id=user_id)
    if not is_safe or filepath is None:
        return {"status": "error", "message": "Access denied or invalid filename."}

    try:
        # Проверяем существование и тип файла
        if not await aiofiles.os.path.exists(filepath):
            return {"status": "error", "message": f"File '{filename}' not found."}
        if await aiofiles.os.path.isdir(filepath):
             return {"status": "error", "message": f"'{filename}' is a directory, cannot edit."}

        # Проверяем размер файла перед чтением
        stat_result = await aiofiles.os.stat(filepath)
        # ----- УДАЛЕНО: Проверка максимального размера чтения -----
        # if stat_result.st_size > settings.max_read_size_bytes:
        #      return {"status": "error", "message": f"File '{filename}' size exceeds read limit ({settings.max_read_size_bytes // 1024} KB)."}
        # ---------------------------------------------------------

        # Читаем файл
        async with aiofiles.open(filepath, mode="r", encoding='utf-8', errors='ignore') as f:
            content = await f.read()

        # Выполняем замену
        new_content = content.replace(search_string, replace_string)

        if new_content == content:
            logger.warning(f"{tool_name}: Search string '{search_string}' not found in file '{filename}'. No changes made.")
            return {"status": "warning", "message": f"Search string not found in '{filename}'. No changes made."}

        # Проверяем размер НОВОГО контента перед записью
        new_content_bytes = new_content.encode('utf-8')
        # ----- УДАЛЕНО: Проверка максимального размера записи -----
        # if len(new_content_bytes) > settings.max_write_size_bytes:
        #     return {"status": "error", "message": f"Edited content size ({len(new_content_bytes)} bytes) would exceed write limit ({settings.max_write_size_bytes // 1024} KB)."}
        # ---------------------------------------------------------

        # Записываем измененный файл
        async with aiofiles.open(filepath, mode="w", encoding='utf-8') as f:
            await f.write(new_content)

        logger.info(f"{tool_name}: Successfully edited file '{filename}'. Replaced '{search_string}' with '{replace_string}'.")
        return {"status": "success", "message": f"File '{filename}' edited successfully."}

    except Exception as e:
        msg = f"Error editing file '{filename}': {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg}


async def replace_code_block_ast(
    user_id: int, chat_id: int, filename: str, block_type: str, block_name: str, new_code_block: str
) -> Dict[str, str]:
    """
    Заменяет блок кода (функцию или класс) в Python-файле окружения чата, используя AST.
    Выполняет файловые операции асинхронно с помощью aiofiles.

    Args:
        user_id: ID пользователя.
        chat_id: ID текущего чата.
        filename: Относительный путь к .py файлу.
        block_type: 'function' или 'class'.
        block_name: Имя заменяемого блока.
        new_code_block: Полный код нового блока.

    Returns:
        dict: Словарь со статусом операции.
    """
    tool_name = "replace_code_block_ast"
    logger.info(f"--- Tool Call: {tool_name}(user={user_id}, chat={chat_id}, file='{filename}', type='{block_type}', name='{block_name}') ---")

    # Проверка версии Python (ast.unparse доступен с 3.9) - остается синхронной
    import sys
    if sys.version_info < (3, 9):
        return {"status": "error", "message": "Error: This tool requires Python 3.9+ for ast.unparse()."}

    if aiofiles is None:
         return {"status": "error", "message": "Internal error: aiofiles library missing."}

    # Проверяем путь и убеждаемся, что директория чата существует
    is_safe, filepath = await get_safe_chat_path(chat_id, filename, user_id=user_id, ensure_chat_dir_exists=True)
    if not is_safe or filepath is None:
        return {"status": "error", "message": "Access denied, invalid filename, or failed to ensure base chat directory."}

    # Валидация имени файла и типа блока
    if not filename.lower().endswith(".py"): return {"status": "error", "message": "Error: Can only edit Python (.py) files."}
    if block_type not in ["function", "class"]: return {"status": "error", "message": f"Error: Invalid block_type '{block_type}'. Must be 'function' or 'class'."}

    try:
        # Асинхронно читаем исходный код
        if not await aiofiles.os.path.exists(filepath):
            return {"status": "not_found", "message": f"Error: File '{filename}' not found."}
        if await aiofiles.os.path.isdir(filepath):
            return {"status": "error", "message": f"Error: '{filename}' is a directory."}

        async with aiofiles.open(filepath, "r", encoding='utf-8') as f:
             source_code = await f.read()

        # Парсинг AST (синхронные операции)
        try:
            tree = ast.parse(source_code, filename=filename)
        except (SyntaxError, Exception) as parse_err:
             logger.error(f"{tool_name}: Error parsing original Python file '{filename}': {parse_err}", exc_info=True)
             return {"status": "error", "message": f"Error parsing Python code in '{filename}': {parse_err}"}

        try:
            new_code_tree = ast.parse(new_code_block)
            if not new_code_tree.body or len(new_code_tree.body) != 1:
                 return {"status": "error", "message": "Error: 'new_code_block' must contain exactly one function or class definition."}
            new_node = new_code_tree.body[0]

            # Проверка типа нового узла
            if block_type == "function" and not isinstance(new_node, ast.FunctionDef):
                 return {"status": "error", "message": f"Error: Expected 'function' definition, found {type(new_node)}."}
            if block_type == "class" and not isinstance(new_node, ast.ClassDef):
                 return {"status": "error", "message": f"Error: Expected 'class' definition, found {type(new_node)}."}

        except (SyntaxError, Exception) as new_parse_err:
            logger.error(f"{tool_name}: Error parsing new_code_block: {new_parse_err}", exc_info=True)
            return {"status": "error", "message": f"Error parsing new_code_block: {new_parse_err}"}

        # Применяем AST-трансформер (синхронно)
        # Убедимся, что ReplaceCodeTransformer импортирован корректно
        if 'ReplaceCodeTransformer' not in globals() or not callable(ReplaceCodeTransformer):
             # Это может произойти, если _ast_transformer.py не был создан/импортирован
             logger.critical(f"{tool_name}: ReplaceCodeTransformer class not available. Check import.")
             return {"status": "error", "message": "Internal error: Code transformer not available."}

        transformer = ReplaceCodeTransformer(block_type, block_name, new_node)
        new_tree = transformer.visit(tree)

        if not transformer.replaced:
            return {"status": "not_found", "message": f"Error: {block_type.capitalize()} '{block_name}' not found in file '{filename}'."}

        # Генерируем новый код (синхронно)
        new_source_code = ast.unparse(new_tree)

         # Проверка лимита на запись
        try:
            content_bytes = new_source_code.encode('utf-8')
            if len(content_bytes) > settings.max_write_size_bytes:
                 msg = f"Error: Resulting code size ({len(content_bytes)} bytes) exceeds limit ({settings.max_write_size_bytes // 1024} KB)."
                 logger.error(f"{tool_name}: {msg} for file '{filename}' after AST edit.")
                 return {"status": "error", "message": msg + " Edit aborted."}
        except Exception as e:
             logger.error(f"{tool_name}: Error encoding modified code for size check: {e}")
             return {"status": "error", "message": "Error checking modified code size."}

        # Асинхронно записываем новый код в файл
        async with aiofiles.open(filepath, "w", encoding='utf-8') as f:
            await f.write(new_source_code)

        msg = f"Successfully replaced {block_type} '{block_name}' in file '{filename}' using AST."
        logger.info(f"{tool_name}: {msg}")
        return {"status": "success", "message": msg}

    except Exception as e:
        msg = f"Error during AST replacement for file '{filename}': {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg}


async def edit_json_file(
    user_id: int, chat_id: int, filename: str, json_path: str, new_value_json: str
) -> Dict[str, str]:
    """
    Редактирует JSON-файл в окружении чата по указанному пути.
    Выполняет файловые операции асинхронно.

    Args:
        user_id: ID пользователя.
        chat_id: ID текущего чата.
        filename: Относительный путь к .json файлу.
        json_path: Путь к элементу (dot-нотация, например 'a.b[0].c').
        new_value_json: Новое значение в виде JSON-строки.

    Returns:
        dict: Словарь со статусом операции.
    """
    tool_name = "edit_json_file"
    logger.info(f"--- Tool Call: {tool_name}(user={user_id}, chat={chat_id}, file='{filename}', path='{json_path}') ---")

    if aiofiles is None: return {"status": "error", "message": "Internal error: aiofiles library missing."}

    # Проверяем путь и убеждаемся, что директория чата существует
    is_safe, filepath = await get_safe_chat_path(chat_id, filename, user_id=user_id, ensure_chat_dir_exists=True)
    if not is_safe or filepath is None:
        return {"status": "error", "message": "Access denied, invalid filename, or failed to ensure base chat directory."}

    # Предупреждаем, если расширение не .json, но продолжаем
    if not filename.lower().endswith(".json"):
        logger.warning(f"{tool_name}: File '{filename}' does not have .json extension.")

    try:
        # Асинхронно читаем JSON
        if not await aiofiles.os.path.exists(filepath): return {"status": "not_found", "message": f"Error: File '{filename}' not found."}
        if await aiofiles.os.path.isdir(filepath): return {"status": "error", "message": f"Error: '{filename}' is a directory."}

        async with aiofiles.open(filepath, "r", encoding='utf-8') as f:
            try:
                content = await f.read()
                data = json.loads(content) # Парсинг JSON (синхронный)
            except json.JSONDecodeError as e:
                logger.error(f"{tool_name}: JSON Decode Error reading file '{filename}': {e}")
                return {"status": "error", "message": f"Error decoding JSON from file '{filename}': {e}"}
            except Exception as read_err: # Ловим и другие ошибки чтения
                 logger.error(f"Error reading file content '{filename}': {read_err}", exc_info=True)
                 return {"status": "error", "message": f"Error reading file: {read_err}"}


        # Парсинг нового значения (синхронный)
        try:
            new_value = json.loads(new_value_json)
        except json.JSONDecodeError:
            # Если не JSON, используем как строку (поведение как в v3)
            logger.warning(f"{tool_name}: Could not parse new_value_json '{new_value_json}' as JSON. Using as raw string.")
            new_value = new_value_json # Оставляем строкой

        # --- Логика парсинга json_path и установки значения (синхронная) ---
        # (Код парсинга и установки значения остается без изменений, как в v3)
        keys: List[Any] = []
        current_part = ""
        in_bracket = False
        in_quotes = False
        quote_char = ''
        path_iterator = iter(enumerate(json_path))
        try:
             for i, char in path_iterator:
                  if char in ('"', "'") and not in_bracket:
                       if not in_quotes: in_quotes = True; quote_char = char;
                       elif char == quote_char:
                            in_quotes = False; keys.append(current_part); current_part = ""
                            next_i = i + 1
                            if next_i < len(json_path) and json_path[next_i] == '.': next(path_iterator, None)
                       else: current_part += char
                       continue
                  if in_quotes: current_part += char; continue
                  if char == '.' and not in_bracket:
                       if not current_part and not keys: raise ValueError("JSON path cannot start with '.'")
                       if not current_part: raise ValueError(f"Invalid JSON path near index {i} (empty key)")
                       keys.append(current_part); current_part = ""
                  elif char == '[' and not in_bracket:
                       if current_part: keys.append(current_part); current_part = ""
                       in_bracket = True
                  elif char == ']' and in_bracket:
                       keys.append(int(current_part)); current_part = ""; in_bracket = False
                       next_i = i + 1
                       if next_i < len(json_path) and json_path[next_i] == '.': next(path_iterator, None)
                  elif in_bracket:
                       if not char.isdigit(): raise ValueError(f"Non-digit char '{char}' inside list index")
                       current_part += char
                  else: current_part += char
             if in_quotes: raise ValueError(f"Unmatched quote '{quote_char}' in JSON path")
             if in_bracket: raise ValueError("Unmatched '[' in JSON path")
             if current_part: keys.append(current_part)
             if not keys: raise ValueError("Empty or invalid JSON path")
        except (ValueError, IndexError) as path_err:
             logger.error(f"{tool_name}: Error parsing JSON path '{json_path}': {path_err}")
             return {"status": "error", "message": f"Error parsing JSON path '{json_path}': {path_err}"}

        temp_data: Any = data
        try:
             for key in keys[:-1]:
                  if isinstance(temp_data, list): temp_data = temp_data[int(key)]
                  elif isinstance(temp_data, dict): temp_data = temp_data[str(key)]
                  else: raise TypeError(f"Cannot access key '{key}' on element type {type(temp_data)}")
             last_key = keys[-1]
             if isinstance(temp_data, list): temp_data[int(last_key)] = new_value
             elif isinstance(temp_data, dict): temp_data[str(last_key)] = new_value
             elif len(keys) == 1: data = new_value; logger.warning(f"{tool_name}: Overwrote entire JSON file '{filename}'")
             else: raise TypeError(f"Cannot set value at '{last_key}'. Parent is not list/dict.")
        except (IndexError, KeyError, ValueError, TypeError) as set_err:
             logger.error(f"{tool_name}: Error setting value at path '{json_path}': {set_err}")
             return {"status": "error", "message": f"Error setting value at path '{json_path}': {set_err}"}
        # --- Конец логики парсинга и установки ---

        # Асинхронно записываем измененные данные обратно
        try:
            modified_json_str = json.dumps(data, indent=4, ensure_ascii=False)
            # Проверка лимита на запись
            content_bytes = modified_json_str.encode('utf-8')
            if len(content_bytes) > settings.max_write_size_bytes:
                 msg = f"Error: Resulting JSON size ({len(content_bytes)} bytes) exceeds limit ({settings.max_write_size_bytes // 1024} KB)."
                 logger.error(f"{tool_name}: {msg} for file '{filename}' after JSON edit.")
                 return {"status": "error", "message": msg + " Edit aborted."}

            async with aiofiles.open(filepath, "w", encoding='utf-8') as f:
                await f.write(modified_json_str)

            msg = f"JSON file '{filename}' edited successfully at path '{json_path}'."
            logger.info(f"{tool_name}: {msg}")
            return {"status": "success", "message": msg}
        except TypeError as e: # Ошибка сериализации измененных данных
             logger.error(f"{tool_name}: Error serializing modified JSON data for '{filename}': {e}", exc_info=True)
             return {"status": "error", "message": f"Error serializing modified JSON data: {e}"}
        except Exception as write_err: # Другие ошибки записи
             logger.error(f"{tool_name}: Error writing JSON file '{filename}': {write_err}", exc_info=True)
             return {"status": "error", "message": f"Error writing JSON file: {write_err}"}

    except Exception as e:
        # Ловим ошибки чтения файла или другие непредвиденные
        msg = f"Error processing JSON file '{filename}': {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg}


async def send_file_from_env(user_id: int, chat_id: int, filename: str) -> Dict[str, str]:
    """
    Отправляет файл из окружения чата пользователю в текущий чат.

    Args:
        user_id (int): ID пользователя.
        chat_id (int): ID текущего чата для отправки файла.
        filename (str): Относительный путь к файлу в окружении.

    Returns:
        dict: Словарь со статусом операции.
    """
    tool_name = "send_file_from_env"
    logger.info(f"--- Tool Call: {tool_name}(user={user_id}, chat={chat_id}, file='{filename}') ---")

    if aiofiles is None:
        return {"status": "error", "message": "Internal error: aiofiles library missing."}

    # Проверяем путь БЕЗ создания директории чата
    is_safe, filepath = await get_safe_chat_path(chat_id, filename, user_id=user_id, ensure_chat_dir_exists=False)
    if not is_safe or filepath is None:
        return {"status": "error", "message": "Access denied or invalid filename."}

    try:
        # Проверяем существование и тип файла асинхронно
        if not await aiofiles.os.path.exists(filepath):
            return {"status": "not_found", "message": f"File '{filename}' not found in environment."}
        if await aiofiles.os.path.isdir(filepath):
             return {"status": "error", "message": f"'{filename}' is a directory, cannot send it as a file."}

        # Отправляем файл
        from bot_loader import bot # Импортируем бота здесь, чтобы избежать циклических зависимостей на уровне модуля
        if bot is None:
            return {"status": "error", "message": "Internal error: Bot instance is unavailable."}

        try:
            input_file = FSInputFile(filepath) # Создаем объект для отправки
            await bot.send_document(chat_id=chat_id, document=input_file)
            logger.info(f"{tool_name}: Successfully sent file '{filename}' to chat {chat_id}.")
            return {"status": "success", "message": f"File '{filename}' sent successfully."}
        except Exception as send_error:
             # Ловим ошибки отправки (файл слишком большой, бот заблокирован и т.д.)
             logger.error(f"{tool_name}: Failed to send file '{filename}' to chat {chat_id}: {send_error}", exc_info=True)
             # Попробуем извлечь более конкретное сообщение об ошибке TelegramAPIError
             from aiogram.exceptions import TelegramAPIError
             error_details = str(send_error)
             if isinstance(send_error, TelegramAPIError):
                  error_details = send_error.message # Более специфичное сообщение
             return {"status": "error", "message": f"Failed to send file: {error_details}"}

    except Exception as e:
        msg = f"Error preparing to send file '{filename}': {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg}