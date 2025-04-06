# tools/__init__.py
import logging
import asyncio
import importlib
import inspect # <<< ДОБАВИТЬ ИМПОРТ inspect >>>
from typing import Dict, Callable, Coroutine, Any

available_functions: Dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}
logger = logging.getLogger(__name__)
logger.info("Initializing tools...")

tool_module_paths = [
    ".basic_tools",
    ".communication_tools",
    ".user_data_tools",
    ".environment_tools",
    ".deep_search_tool",
    ".meta_tools"
]

internal_function_names = {
    "get_safe_chat_path",
    "ensure_chat_dir",
}
disabled_tool_names = {}

for module_path in tool_module_paths:
    try:
        module = importlib.import_module(module_path, package=__name__)
        found_in_module = 0
        logger.debug(f"Processing tool module: {module_path}")

        # <<< ДОБАВИТЬ ЭТИ ЛОГИ >>>
        logger.debug(f"Module object for {module_path}: {module}")
        try:
            module_members = inspect.getmembers(module)
            logger.debug(f"Members found in {module_path} via inspect: {len(module_members)}")
            # Выведем несколько первых членов для примера
            for name, obj_type in module_members[:15]:
                 logger.debug(f"  - Member: name='{name}', type='{type(obj_type)}'")
        except Exception as inspect_err:
             logger.error(f"Failed to inspect members of {module_path}: {inspect_err}")
        # <<< КОНЕЦ ДОБАВЛЕНИЯ >>>

        # --- Старый цикл через __dict__ (оставляем пока) ---
        logger.debug(f"Attempting iteration via module.__dict__ for {module_path}")
        items_in_dict = 0
        for func_name, func_obj in module.__dict__.items():
            items_in_dict += 1
            # Предыдущий отладочный лог
            logger.debug(f"Checking item from {module_path}.__dict__: name='{func_name}', is_coroutine={asyncio.iscoroutinefunction(func_obj)}")

            if (asyncio.iscoroutinefunction(func_obj) and
                    not func_name.startswith('_') and
                    func_name not in internal_function_names and
                    func_name not in disabled_tool_names):
                if func_name in available_functions:
                    logger.warning(f"Duplicate tool function name '{func_name}' found in {module_path}. Overwriting.")
                available_functions[func_name] = func_obj
                found_in_module += 1
        logger.debug(f"Finished iterating {items_in_dict} items from {module_path}.__dict__.")
        # --- Конец старого цикла ---


        if found_in_module > 0:
            logger.info(f"Registered {found_in_module} tools from {module_path}.")
        else:
             logger.debug(f"No valid async tool functions found or registered in {module_path}.")

    except ImportError as e:
        logger.warning(f"Could not import tool module {module_path}: {e}.")
    except Exception as e:
         logger.error(f"Unexpected error processing module {module_path}: {e}", exc_info=True)

logger.info(f"Total initialized tools: {len(available_functions)}. Names: {list(available_functions.keys())}")
__all__ = ["available_functions"]