import logging
import asyncio
import inspect
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("TEST_IMPORT")
logger.info("--- Starting Import Test ---")
try:
    # Попытка импорта точно так же, как в __init__.py
    env_module = __import__("tools.environment_tools", fromlist=[''])
    logger.info(f"Import successful. Module object: {env_module}")

    # Проверка содержимого через inspect
    members_found = 0
    coroutines_found = 0
    print("\n--- Inspecting Members ---")
    for name, obj in inspect.getmembers(env_module):
         members_found += 1
         is_coro = asyncio.iscoroutinefunction(obj)
         print(f"Name: {name:<40} | Type: {type(obj).__name__:<30} | Is Coroutine: {is_coro}")
         if is_coro: coroutines_found += 1
    print(f"\n--- Found {members_found} members, {coroutines_found} coroutine functions ---")

    # Проверка содержимого через __dict__
    print("\n--- Iterating __dict__ ---")
    dict_items = 0
    dict_coroutines = 0
    for name, obj in env_module.__dict__.items():
        dict_items += 1
        is_coro = asyncio.iscoroutinefunction(obj)
        if is_coro: dict_coroutines +=1
        print(f"Name: {name:<40} | Is Coroutine: {is_coro}")
    print(f"\n--- Found {dict_items} items in __dict__, {dict_coroutines} coroutine functions ---")


except ImportError as e:
    logger.critical(f"CRITICAL: Failed to import tools.environment_tools: {e}", exc_info=True)
except Exception as e:
    logger.critical(f"CRITICAL: Unexpected error during import/inspection: {e}", exc_info=True)
logger.info("--- Import Test Finished ---")

# Запусти этот файл: python test_import.py