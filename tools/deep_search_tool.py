# tools/deep_search_tool.py

import asyncio
import logging
import os
import re
import time # Для задержек между поисками/API вызовами
from typing import Dict, Optional, List, Tuple, Any

# --- Локальные Зависимости ---
try:
    # AI интерфейс для вызовов Gemini
    from ai_interface import gemini_api # Нужна функция для генерации без сессии
    from bot_loader import dp # Для доступа к модели из workflow_data
    # Утилиты
    from utils.helpers import remove_markdown
    # Настройки
    from config import settings
except ImportError:
    logging.critical("CRITICAL: Failed to import dependencies (gemini_api, dp, helpers, settings) in deep_search_tool.", exc_info=True)
    # Заглушки
    gemini_api = None # type: ignore
    dp = type('obj', (object,), {'workflow_data': {}})() # type: ignore
    def remove_markdown(text: str) -> str: return text
    settings = type('obj', (object,), {'google_api_key': None, 'deep_search_prompts_dir': Path('prompts/deep_search')})() # type: ignore

# Веб-поиск
try:
    from duckduckgo_search import DDGS, AsyncDDGS # Попробуем импортировать обе версии
    HAS_ASYNC_DDGS = True
except ImportError:
    try: # Попробуем только синхронную
        from duckduckgo_search import DDGS
        AsyncDDGS = None
        HAS_ASYNC_DDGS = False
    except ImportError:
        logging.error("duckduckgo-search library not found. Web search unavailable.")
        DDGS = None
        AsyncDDGS = None
        HAS_ASYNC_DDGS = False

# Асинхронные файлы
try:
    import aiofiles.os
except ImportError:
    aiofiles = None # type: ignore
    logging.error("aiofiles library not found. Prompt loading might fail.")

from pathlib import Path

logger = logging.getLogger(__name__)

# --- Константы (можно уточнить в config.py) ---
DEFAULT_ITERATIONS = 2
QUESTIONS_PER_ITERATION = 5
NUM_SEARCH_RESULTS = 3
MAX_SEARCH_CONTEXT_LEN = 8000 # Ограничение контекста поиска
REPORT_CONTEXT_SNIPPET_LEN = 3000 # Сколько предыдущего отчета давать в контекст
SEARCH_DELAY = 1 # Уменьшаем задержку для асинхронного поиска
API_DELAY = 2    # Задержка между вызовами API

# Путь к промптам DeepSearch из настроек
PROMPTS_DIR = settings.deep_search_prompts_dir if hasattr(settings, 'deep_search_prompts_dir') else Path("prompts/deep_search")

# --- Вспомогательные функции ---

def _parse_questions(text: Optional[str]) -> List[str]:
    """Извлекает нумерованные вопросы или строки из текста."""
    if not text: return []
    # Ищем строки, начинающиеся с цифры и точки/скобки, или маркеров списка
    questions = re.findall(r"^\s*[\d]+[.)]*\s*(.+)$|^\s*[-*+]\s*(.+)$", text, re.MULTILINE)
    parsed = [q[0] or q[1] for q in questions if q[0] or q[1]] # Берем непустую группу
    cleaned = [q.strip() for q in parsed if q.strip()]
    if not cleaned and '\n' in text: # Если нумерации/маркеров нет, но есть переносы строк
        cleaned = [line.strip() for line in text.splitlines() if line.strip()]
        logger.debug("Parsing questions as lines (no numbering/markers found).")
    logger.debug(f"Parsed {len(cleaned)} questions from text.")
    return cleaned

async def _perform_web_search_async(query: str, num_results: int) -> str:
    """Асинхронно выполняет веб-поиск и возвращает форматированный текст."""
    if not query: return "(Empty search query)"
    logger.debug(f"Performing web search for: '{query[:80]}...'")
    results_text = ""

    if HAS_ASYNC_DDGS and AsyncDDGS:
        try:
            async with AsyncDDGS(timeout=10) as ddgs: # Устанавливаем таймаут
                results = []
                async for r in ddgs.atext(query, max_results=num_results):
                    results.append(r)
            if results:
                for i, r in enumerate(results):
                    title = r.get('title', 'N/A')
                    snippet = remove_markdown(r.get('body', 'N/A')).replace('\n', ' ').strip()
                    results_text += f"  Res {i+1}: {title}\n  Snip: {snippet}\n\n"
            else: results_text = "  (No results found)\n\n"
        except Exception as e:
            logger.error(f"Async web search failed for '{query[:80]}...': {e}", exc_info=True)
            results_text = f"(Async search error: {e})"
    elif DDGS:
        # Используем синхронную версию в executor'е
        def _sync_search():
            sync_results_text = ""
            try:
                # time.sleep(SEARCH_DELAY) # Задержка не нужна, т.к. executor
                with DDGS(timeout=10) as ddgs:
                    search_results = list(ddgs.text(query, max_results=num_results))
                if search_results:
                    for i, r in enumerate(search_results):
                        title = r.get('title', 'N/A')
                        snippet = remove_markdown(r.get('body', 'N/A')).replace('\n', ' ').strip()
                        sync_results_text += f"  Res {i+1}: {title}\n  Snip: {snippet}\n\n"
                else: sync_results_text = "  (No results found)\n\n"
                return sync_results_text.strip()
            except Exception as e:
                logger.error(f"Sync web search failed for '{query[:80]}...': {e}", exc_info=True)
                return f"(Search error: {e})"

        loop = asyncio.get_running_loop()
        try:
            # Добавляем небольшую задержку перед запуском в executor
            await asyncio.sleep(SEARCH_DELAY)
            results_text = await loop.run_in_executor(None, _sync_search)
        except Exception as e:
            logger.error(f"Error running sync search in executor: {e}", exc_info=True)
            results_text = f"(Executor search error: {e})"
    else:
         results_text = "(Web search unavailable: library not found)"

    return results_text

async def _load_prompt_async(filename: str) -> Optional[str]:
     """Асинхронно загружает текст промпта из директории PROMPTS_DIR."""
     if aiofiles is None:
          logger.error("Cannot load prompt: aiofiles library missing.")
          return None
     filepath = PROMPTS_DIR / filename
     try:
          if not await aiofiles.os.path.isfile(filepath):
               logger.error(f"Prompt file not found or is not a file: {filepath}")
               return None
          async with aiofiles.open(filepath, mode="r", encoding="utf-8") as f:
               content = await f.read()
               logger.debug(f"Loaded prompt: {filename}")
               return content.strip()
     except Exception as e:
          logger.error(f"Error reading prompt file {filepath}: {e}", exc_info=True)
          return None

async def _call_gemini_generate(prompt: str, step_description: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Вызывает модель Gemini Pro для генерации текста БЕЗ ИСТОРИИ.
    Возвращает (сгенерированный_текст, сообщение_об_ошибке).
    """
    model = dp.workflow_data.get("pro_model") # Получаем Pro модель
    api_key = getattr(settings, 'google_api_key', None)

    if not model: return None, "AI Pro model instance not available."
    if not api_key: return None, "Google API Key is not configured."
    if not gemini_api or not hasattr(gemini_api, 'genai'): # Проверяем наличие genai
         return None, "Gemini API module not properly loaded."

    logger.debug(f"Calling Gemini for: {step_description} (Prompt length: {len(prompt)})")
    try:
        # Используем прямой вызов generate_content_async модели
        # Настройки генерации и безопасности должны быть встроены в model при инициализации
        await asyncio.sleep(API_DELAY) # Задержка перед вызовом
        response = await model.generate_content_async(prompt)

        if response and response.text:
            logger.debug(f"Gemini response received for: {step_description}")
            return response.text.strip(), None
        elif response and response.prompt_feedback and response.prompt_feedback.block_reason:
             reason = response.prompt_feedback.block_reason
             msg = f"Gemini request blocked for '{step_description}'. Reason: {reason}"
             logger.error(msg)
             return None, msg
        else:
             msg = f"Gemini returned empty or invalid response for '{step_description}'."
             logger.error(f"{msg} Response: {response}")
             return None, msg

    except Exception as e:
        msg = f"Error calling Gemini API for '{step_description}': {e}"
        logger.error(msg, exc_info=True)
        return None, msg


# --- Основная функция инструмента ---

async def refine_text_with_deep_search(
    topic: Optional[str] = None,
    initial_text: Optional[str] = None,
    iterations: Optional[int] = None,
    user_prompt_guidance: Optional[str] = None
) -> Dict[str, Any]:
    """
    Итеративно улучшает или генерирует текст, используя веб-поиск и AI.

    Args:
        topic (Optional[str]): Тема для генерации (если initial_text не предоставлен).
        initial_text (Optional[str]): Исходный текст для улучшения.
        iterations (Optional[int]): Количество итераций улучшения (по умолчанию DEFAULT_ITERATIONS).
        user_prompt_guidance (Optional[str]): Дополнительные инструкции от пользователя.

    Returns:
        dict: Словарь со статусом ('success', 'warning', 'error') и результатом ('refined_text' или 'message').
    """
    tool_name = "refine_text_with_deep_search"
    logger.info(f"--- Tool Call: {tool_name}(topic='{topic}', initial_text_len={len(initial_text or '')}, iters={iterations}) ---")

    if not initial_text and not topic:
        return {"status": "error", "message": "Either 'topic' or 'initial_text' must be provided."}

    num_iterations = iterations if isinstance(iterations, int) and 0 < iterations < 5 else DEFAULT_ITERATIONS
    logger.info(f"Running Deep Search with {num_iterations} refinement iteration(s).")

    # --- Шаг 0: Генерация начального текста (если не предоставлен) ---
    current_report = initial_text
    if not current_report:
        logger.info(f"Generating initial text for topic: '{topic}'")
        initial_prompt_template = await _load_prompt_async("03_synthesize_report.prompt")
        if not initial_prompt_template: return {"status": "error", "message": "Failed to load initial synthesis prompt."}

        initial_gen_prompt = initial_prompt_template.format(
            original_report=f"Тема: {topic}\n{user_prompt_guidance or ''}\nНапиши начальную версию текста.",
            answers="(Нет данных для улучшения на этом шаге)"
        ).strip()

        current_report, error_msg = await _call_gemini_generate(initial_gen_prompt, "Initial Text Generation")
        if error_msg or not current_report:
            return {"status": "error", "message": f"Failed to generate initial text: {error_msg}"}
        logger.info(f"Initial text generated (len={len(current_report)}).")

    # --- Шаг 1-N: Итеративное улучшение ---
    final_report = current_report
    all_steps_succeeded = True
    iteration = 0 # Инициализируем до цикла

    for i in range(num_iterations):
        iteration = i + 1
        logger.info(f"--- Starting Refinement Iteration {iteration}/{num_iterations} ---")

        # --- 1a: Генерация вопросов ---
        q_template_index = (iteration - 1) % 4 # 4 шаблона вопросов
        q_template_filename = f"01{chr(ord('a') + q_template_index)}_generate_questions.prompt" # Общее имя для простоты
        question_prompt_template = await _load_prompt_async(q_template_filename)
        if not question_prompt_template:
            logger.error(f"Failed to load question prompt template: {q_template_filename}")
            all_steps_succeeded = False; break

        question_gen_prompt = question_prompt_template.format(
            report_text=final_report[-REPORT_CONTEXT_SNIPPET_LEN:], # Даем хвост отчета
            num_questions=QUESTIONS_PER_ITERATION
        ).strip()
        if user_prompt_guidance: question_gen_prompt += f"\n\nДополнительные указания: {user_prompt_guidance}"

        questions_text, error_msg = await _call_gemini_generate(question_gen_prompt, f"Iter {iteration}: Question Gen")
        if error_msg or not questions_text:
            logger.error(f"Failed question generation iter {iteration}: {error_msg}")
            all_steps_succeeded = False; break
        logger.info(f"Iter {iteration}: Questions generated.")
        parsed_questions = _parse_questions(questions_text)
        if not parsed_questions:
            logger.warning(f"Iter {iteration}: No questions parsed. Skipping search and answers.")
            continue # Пропускаем поиск/ответы, но не прерываем цикл

        # --- 1b: Поиск ---
        logger.info(f"Iter {iteration}: Performing web search for {len(parsed_questions)} questions...")
        search_tasks = [_perform_web_search_async(q, NUM_SEARCH_RESULTS) for q in parsed_questions]
        search_results_list = await asyncio.gather(*search_tasks) # Собираем результаты поиска
        search_context = "\n\n".join(
            f"Результаты по вопросу \"{parsed_questions[j]}\":\n{res}"
            for j, res in enumerate(search_results_list) if res and "(Search error" not in res
        ).strip()
        # Обрезаем контекст поиска
        if len(search_context.encode('utf-8', errors='ignore')) > MAX_SEARCH_CONTEXT_LEN:
            search_context_bytes = search_context.encode('utf-8', errors='ignore')
            search_context = search_context_bytes[:MAX_SEARCH_CONTEXT_LEN].decode('utf-8', errors='ignore') + "...(search context truncated)"
            logger.warning(f"Iter {iteration}: Search context truncated to {MAX_SEARCH_CONTEXT_LEN} bytes.")
        logger.info(f"Iter {iteration}: Search completed. Context length: {len(search_context)}")

        # --- 1c: Ответы на вопросы ---
        answer_prompt_template = await _load_prompt_async("02_answer_questions_with_search.prompt")
        if not answer_prompt_template:
            logger.error("Failed to load answer prompt template.")
            all_steps_succeeded = False; break

        answer_gen_prompt = answer_prompt_template.format(
            questions=questions_text,
            search_context=search_context if search_context else "(Информация из поиска недоступна)",
            report_context=final_report[-REPORT_CONTEXT_SNIPPET_LEN:] # Даем хвост предыдущего отчета
        ).strip()
        if user_prompt_guidance: answer_gen_prompt += f"\n\nДополнительные указания: {user_prompt_guidance}"

        answers_text, error_msg = await _call_gemini_generate(answer_gen_prompt, f"Iter {iteration}: Answer Gen")
        if error_msg or not answers_text:
            logger.error(f"Failed answer generation iter {iteration}: {error_msg}")
            all_steps_succeeded = False; break
        logger.info(f"Iter {iteration}: Answers generated.")

        # --- 1d: Синтез ---
        synthesis_prompt_template = await _load_prompt_async("03_synthesize_report.prompt")
        if not synthesis_prompt_template:
             logger.error("Failed to load synthesis prompt template.")
             all_steps_succeeded = False; break

        synthesis_prompt = synthesis_prompt_template.format(
            original_report=final_report,
            answers=answers_text
        ).strip()
        if user_prompt_guidance: synthesis_prompt += f"\n\nДополнительные указания: {user_prompt_guidance}"

        new_report, error_msg = await _call_gemini_generate(synthesis_prompt, f"Iter {iteration}: Synthesis")
        if error_msg or not new_report:
            logger.error(f"Failed synthesis iter {iteration}: {error_msg}")
            all_steps_succeeded = False; break

        logger.info(f"Iter {iteration}: Synthesis complete. New length: {len(new_report)}")
        final_report = new_report # Обновляем отчет для следующей итерации

    # --- Завершение ---
    if not final_report:
        return {"status": "error", "message": "Failed to produce any text content."}

    if all_steps_succeeded:
        msg = f"Text refinement completed successfully after {num_iterations} iteration(s)."
        logger.info(msg)
        return {"status": "success", "refined_text": final_report, "message": msg}
    else:
        msg = f"Text refinement finished with errors after iteration {iteration}. Returning last successful version."
        logger.warning(msg)
        # Возвращаем последний успешный вариант отчета
        return {"status": "warning", "refined_text": final_report, "message": msg}