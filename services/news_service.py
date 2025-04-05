# services/news_service.py

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional, Set
import re

# --- Сторонние зависимости ---
import aiohttp # Для асинхронных HTTP запросов
import feedparser # Для парсинга RSS
from bs4 import BeautifulSoup # Для извлечения данных из HTML

# --- Aiogram ---
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramAPIError
from aiogram.utils.markdown import hlink # Для форматирования ссылок

# --- Локальные импорты ---
try:
    from config import settings # Настройки, включая RSS_MAPPING
    import database # Функции для работы с БД (подписки, guids)
    from utils.helpers import remove_markdown # Утилита для очистки Markdown
except ImportError:
    logging.critical("CRITICAL: Failed to import dependencies (config, database, utils.helpers) in news_service.", exc_info=True)
    # Заглушки на случай ошибки импорта
    class MockSettings: rss_mapping: Dict[str, List[str]] = {}
    settings = MockSettings()
    database = None # type: ignore
    def remove_markdown(text: str) -> str: return text

logger = logging.getLogger(__name__)

# --- Константы ---
CHECK_INTERVAL_SECONDS = 60 # Как часто проверять расписание
GUID_CLEANUP_INTERVAL_HOURS = 24 # Как часто чистить старые GUIDы
GUID_TTL_DAYS = 7 # Сколько дней хранить GUIDы
RECENT_GUID_LOAD_DAYS = 7 # За сколько дней загружать GUIDы при старте
REQUEST_TIMEOUT = 15 # Таймаут для HTTP запросов
MAX_NEWS_PER_TOPIC_PER_RUN = 1 # Сколько новостей одного топика отправлять за раз
MAX_DESCRIPTION_LENGTH = 3500 # Ограничение длины описания для отправки (до разбивки)

class NewsService:
    def __init__(self):
        self._bot_instance: Optional[Bot] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self.sent_guids: Set[str] = set() # Кэш отправленных GUID в памяти

    async def start(self, bot: Bot):
        """Запускает сервис новостей: загружает данные и планирует задачи."""
        if self._scheduler_task or self._cleanup_task:
            logger.warning("NewsService already started or starting.")
            return

        if database is None:
             logger.error("Cannot start NewsService: database module is unavailable.")
             return

        self._bot_instance = bot
        logger.info("Starting NewsService...")
        await self._load_sent_guids() # Загружаем недавние GUIDы

        # Запускаем фоновые задачи
        self._scheduler_task = asyncio.create_task(self._check_schedule_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_guids_loop())
        logger.info("NewsService scheduler and cleanup tasks started.")

    async def stop(self):
        """Останавливает фоновые задачи сервиса."""
        logger.info("Stopping NewsService...")
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try: await self._scheduler_task
            except asyncio.CancelledError: pass
            self._scheduler_task = None
            logger.info("News scheduler task stopped.")
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try: await self._cleanup_task
            except asyncio.CancelledError: pass
            self._cleanup_task = None
            logger.info("GUID cleanup task stopped.")

    async def _load_sent_guids(self):
        """Загружает недавние отправленные GUIDы из БД в кэш."""
        logger.debug(f"Loading recent sent GUIDs (last {RECENT_GUID_LOAD_DAYS} days)...")
        try:
            self.sent_guids = await database.load_recent_sent_guids(days=RECENT_GUID_LOAD_DAYS)
            logger.info(f"Loaded {len(self.sent_guids)} recent GUIDs into memory cache.")
        except Exception as e:
            logger.error(f"Failed to load recent GUIDs from database: {e}", exc_info=True)
            self.sent_guids = set() # Начинаем с пустым кэшем в случае ошибки

    async def _check_schedule_loop(self):
        """Бесконечный цикл проверки расписания."""
        while True:
            try:
                if self._bot_instance is None:
                     logger.error("NewsService check loop: Bot instance is None. Stopping loop.")
                     break
                await self._process_scheduled_posts(self._bot_instance)
            except asyncio.CancelledError:
                logger.info("News scheduler loop cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in news scheduler loop: {e}", exc_info=True)
            # Ждем перед следующей проверкой
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def _cleanup_guids_loop(self):
        """Бесконечный цикл очистки старых GUIDов."""
        while True:
            try:
                 if database is None:
                     logger.error("NewsService cleanup loop: database module unavailable. Stopping loop.")
                     break
                 logger.info("Running periodic GUID cleanup...")
                 deleted_count = await database.cleanup_old_guids(days=GUID_TTL_DAYS)
                 logger.info(f"GUID cleanup finished. Deleted {deleted_count} old GUIDs.")
                 # Перезагружаем кэш GUIDов после очистки
                 await self._load_sent_guids()
            except asyncio.CancelledError:
                 logger.info("GUID cleanup loop cancelled.")
                 break
            except Exception as e:
                 logger.error(f"Error in GUID cleanup loop: {e}", exc_info=True)
            # Очистка раз в несколько часов
            await asyncio.sleep(GUID_CLEANUP_INTERVAL_HOURS * 60 * 60)


    async def _process_scheduled_posts(self, bot: Bot):
        """Получает все подписки и обрабатывает те, для которых подошло время."""
        if database is None: return
        try:
            now_time_str = datetime.now(timezone.utc).strftime("%H:%M") # Используем UTC для сравнения
            logger.debug(f"Checking subscriptions for schedule time: {now_time_str}")
            subscriptions = await database.get_all_subscriptions()

            tasks = []
            processed_channels = set() # Чтобы не обрабатывать один канал несколько раз за проверку

            for sub in subscriptions:
                channel_id = sub['channel_id']
                if channel_id in processed_channels: continue

                schedule = sub.get('schedule', [])
                last_post_ts = sub.get('last_post_ts') # Это объект datetime или None
                last_post_time_str = last_post_ts.strftime("%H:%M") if last_post_ts else None

                if now_time_str in schedule and now_time_str != last_post_time_str:
                    logger.info(f"Scheduling processing for channel {channel_id} at {now_time_str}")
                    # Запускаем обработку канала как отдельную задачу
                    tasks.append(asyncio.create_task(
                        self._process_channel(bot, channel_id, sub, now_time_str)
                    ))
                    processed_channels.add(channel_id)

            if tasks:
                await asyncio.gather(*tasks) # Ждем завершения обработки всех каналов
                logger.debug(f"Finished processing scheduled tasks for time {now_time_str}.")

        except Exception as e:
            logger.error(f"Critical error during scheduled post processing: {e}", exc_info=True)

    async def _process_channel(self, bot: Bot, channel_id: int, settings: Dict, current_time_str: str):
        """Обрабатывает публикации для одного канала."""
        logger.info(f"Processing news for channel {channel_id}...")
        processed_guids_this_run = set() # GUIDы, обработанные в ЭТОМ запуске для канала
        try:
            topics = settings.get('topics', [])
            all_news_items = []
            fetch_tasks = []

            # Асинхронно получаем новости по всем темам канала
            for topic in topics:
                fetch_tasks.append(asyncio.create_task(self._fetch_news_for_topic(topic)))

            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

            # Собираем все новости, проверяя на ошибки
            for i, result in enumerate(results):
                 if isinstance(result, Exception):
                      logger.error(f"Failed to fetch news for topic '{topics[i]}' in channel {channel_id}: {result}")
                 elif isinstance(result, list):
                      all_news_items.extend(result)

            if not all_news_items:
                 logger.info(f"No new news items found for channel {channel_id} across all topics.")
                 # Обновляем время последней проверки, даже если новостей нет, чтобы не проверять снова в ту же минуту
                 await database.update_subscription_last_post(channel_id, datetime.now(timezone.utc))
                 return

            # Сортируем все новости по времени публикации (самые свежие сначала)
            all_news_items.sort(key=lambda x: x.get("published_parsed", datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

            # Отправляем ограниченное количество самых свежих НОВЫХ новостей
            sent_count = 0
            for news_item in all_news_items:
                guid = news_item.get("guid")
                if guid and guid not in self.sent_guids and guid not in processed_guids_this_run:
                    send_success = await self._send_news_item(bot, channel_id, news_item)
                    if send_success:
                        processed_guids_this_run.add(guid)
                        self.sent_guids.add(guid) # Добавляем в кэш
                        await database.add_sent_guid(guid) # Сохраняем в БД
                        sent_count += 1
                        if sent_count >= MAX_NEWS_PER_TOPIC_PER_RUN * len(topics): # Ограничение на общее кол-во за раз
                            break
                    # Пауза между отправками в один канал
                    await asyncio.sleep(1)

            # Обновляем время последней отправки/проверки в БД
            await database.update_subscription_last_post(channel_id, datetime.now(timezone.utc))
            logger.info(f"Finished processing channel {channel_id}. Sent {sent_count} news items.")

        except TelegramForbiddenError:
            logger.error(f"Bot is blocked or removed from channel {channel_id}. Removing subscription.")
            if database: await database.delete_subscription(channel_id)
        except TelegramBadRequest as e:
            # Частые ошибки: chat not found, user is deactivated
            logger.error(f"Telegram API Bad Request for channel {channel_id}: {e}. Removing subscription if chat not found.")
            if "chat not found" in str(e).lower():
                 if database: await database.delete_subscription(channel_id)
        except Exception as e:
            logger.error(f"Unexpected error processing channel {channel_id}: {e}", exc_info=True)


    async def _fetch_news_for_topic(self, topic: str) -> List[Dict]:
        """Получает и парсит новости по одной теме."""
        if database is None: return []

        rss_urls = settings.rss_mapping.get(topic.lower(), [])
        if not rss_urls:
            logger.warning(f"No RSS URLs found for topic '{topic}'.")
            return []

        all_entries = []
        parse_tasks = []
        logger.debug(f"Fetching news for topic '{topic}' from URLs: {rss_urls}")

        # Асинхронно парсим все RSS ленты для темы
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as session:
            for rss_url in rss_urls:
                if rss_url: # Пропускаем пустые URL
                    parse_tasks.append(asyncio.create_task(self._parse_rss(session, rss_url)))

            results = await asyncio.gather(*parse_tasks, return_exceptions=True)

            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"Failed to parse RSS feed '{rss_urls[i]}': {result}")
                elif isinstance(result, list):
                    all_entries.extend(result)

        return all_entries


    async def _parse_rss(self, session: aiohttp.ClientSession, rss_url: str) -> List[Dict]:
        """Асинхронно загружает и парсит одну RSS ленту."""
        try:
            async with session.get(rss_url) as response:
                response.raise_for_status() # Проверка на HTTP ошибки
                feed_text = await response.text()
                # feedparser работает синхронно, запускаем в executor'е
                loop = asyncio.get_running_loop()
                feed = await loop.run_in_executor(None, feedparser.parse, feed_text)

                if feed.bozo:
                    logger.error(f"RSS parsing error for {rss_url}: {feed.bozo_exception}")
                    return []

                processed_entries = []
                for entry in feed.entries:
                    guid = entry.get("id", entry.get("link")) # GUID или ссылка как идентификатор
                    if not guid:
                         logger.warning(f"Entry in {rss_url} lacks id and link, generating guid.")
                         # Генерируем GUID на основе заголовка и времени (если есть)
                         ts = entry.get("published_parsed", entry.get("updated_parsed", datetime.now(timezone.utc).timetuple()))
                         guid = f"{rss_url}::{entry.get('title','no_title')}::{time.mktime(ts)}"

                    # Проверяем по кэшу в памяти
                    if guid not in self.sent_guids:
                         # Проверяем по БД (на случай, если кэш неполный)
                         if database and not await database.is_guid_sent(guid):
                              entry_data = self._extract_entry_data(entry, rss_url)
                              if entry_data:
                                   entry_data["guid"] = guid # Добавляем GUID в данные
                                   processed_entries.append(entry_data)
                         # else: logger.debug(f"GUID {guid} already sent (checked DB).")
                    # else: logger.debug(f"GUID {guid} already sent (checked memory cache).")

                return processed_entries

        except aiohttp.ClientError as e:
             logger.error(f"HTTP error fetching RSS {rss_url}: {e}")
             return []
        except Exception as e:
            logger.error(f"Error parsing RSS {rss_url}: {e}", exc_info=True)
            return []

    def _extract_entry_data(self, entry: Any, rss_url: str) -> Optional[Dict]:
        """Извлекает и очищает данные из одной записи RSS."""
        try:
            title = getattr(entry, 'title', 'Без заголовка')
            link = getattr(entry, 'link', None)
            if not link: return None # Пропускаем записи без ссылки

            # Извлекаем описание и очищаем HTML
            description_html = getattr(entry, 'description', getattr(entry, 'summary', ''))
            soup = BeautifulSoup(description_html, "html.parser")

            # Удаляем стандартные фразы "Читать далее" и т.п.
            for link_tag in soup.find_all('a'):
                if "читать дал" in link_tag.text.lower() or "read more" in link_tag.text.lower():
                    link_tag.decompose()

            # Ищем изображение
            img_tag = soup.find("img")
            image_url = img_tag["src"] if img_tag and 'src' in img_tag.attrs else ""
            # Fallback для медиа-контента
            if not image_url and hasattr(entry, "media_content") and entry.media_content:
                for media in entry.media_content:
                    if media.get('medium') == 'image' and media.get('url'):
                        image_url = media.get('url')
                        break
            # Fallback для enclosure
            if not image_url and hasattr(entry, "enclosures") and entry.enclosures:
                 for enc in entry.enclosures:
                      if enc.get('type', '').startswith('image/') and enc.get('href'):
                           image_url = enc.get('href')
                           break

            # Получаем чистый текст описания
            clean_text = soup.get_text(separator="\n", strip=True)
            # Ограничиваем длину описания
            if len(clean_text) > MAX_DESCRIPTION_LENGTH:
                 clean_text = clean_text[:MAX_DESCRIPTION_LENGTH] + "..."

            # Хештеги
            hashtags = []
            if hasattr(entry, "tags"):
                hashtags = ["#" + tag.term.replace(" ", "_").replace("-", "_") for tag in entry.tags if tag.term]
            elif hasattr(entry, "category"):
                # У category может быть строка, а не объект term
                 cat_term = getattr(entry.category, 'term', entry.category)
                 if isinstance(cat_term, str):
                      hashtags = ["#" + cat_term.replace(" ", "_").replace("-", "_")]

            # Время публикации (пытаемся получить как datetime)
            published_time = getattr(entry, 'published_parsed', getattr(entry, 'updated_parsed', None))
            published_dt = datetime.fromtimestamp(time.mktime(published_time), tz=timezone.utc) if published_time else datetime.now(timezone.utc)

            return {
                "title": title.strip(),
                "content": clean_text,
                "image": image_url,
                "link": link,
                "hashtags": hashtags,
                "published_parsed": published_dt # Сохраняем как datetime
            }

        except Exception as e:
            logger.error(f"Error processing entry from {rss_url} (link: {getattr(entry, 'link', 'N/A')}): {e}", exc_info=True)
            return None


    async def _send_news_item(self, bot: Bot, channel_id: int, news_item: Dict) -> bool:
        """Формирует и отправляет одну новость в канал."""
        title = news_item.get("title", "Новость")
        content = news_item.get("content", "")
        link = news_item.get("link", "")
        image_url = news_item.get("image", "")
        hashtags = " ".join(news_item.get("hashtags", []))

        # Формируем текст сообщения с Markdown V2
        # Используем hlink для безопасного форматирования ссылки
        text_parts = [
            f"*{escape_markdown_v2(title)}*\n", # Жирный заголовок
            escape_markdown_v2(content) if content else "",
            f"\n{hlink('Источник', link)}" if link else "", # Ссылка через hlink
            f"\n{escape_markdown_v2(hashtags)}" if hashtags else ""
        ]
        full_text = "\n".join(filter(None, text_parts)).strip() # Убираем пустые строки

        # Ограничиваем общую длину текста (даже если будет фото)
        if len(full_text) > 4096: # Лимит Telegram для сообщений
            # Обрезаем content, сохраняя остальное
            max_content_len = 4096 - (len(text_parts[0]) + len(text_parts[2]) + len(text_parts[3]) + 10) # +10 на разделители и троеточие
            if max_content_len > 100: # Обрезаем, только если остается разумная длина
                 text_parts[1] = escape_markdown_v2(content[:max_content_len] + "...")
            else: # Иначе просто обрезаем весь текст
                 text_parts = [escape_markdown_v2(title)] # Оставляем только заголовок
            full_text = "\n".join(filter(None, text_parts)).strip()

        try:
            if image_url:
                 # Ограничение caption = 1024 символа
                 caption = full_text
                 if len(caption) > 1024:
                      max_content_len = 1024 - (len(text_parts[0]) + len(text_parts[2]) + len(text_parts[3]) + 10)
                      if max_content_len > 50: text_parts[1] = escape_markdown_v2(content[:max_content_len] + "...")
                      else: text_parts = [escape_markdown_v2(title)] # Только заголовок
                      caption = "\n".join(filter(None, text_parts)).strip()

                 await bot.send_photo(
                     chat_id=channel_id,
                     photo=image_url,
                     caption=caption,
                     parse_mode="MarkdownV2" # Используем MarkdownV2
                 )
                 logger.debug(f"Sent news with photo to {channel_id}: {title[:50]}...")
            else:
                 # Отправляем как текст
                 await bot.send_message(
                     chat_id=channel_id,
                     text=full_text,
                     parse_mode="MarkdownV2", # Используем MarkdownV2
                     disable_web_page_preview=True
                 )
                 logger.debug(f"Sent news as text to {channel_id}: {title[:50]}...")
            return True
        except TelegramAPIError as e:
            logger.error(f"Failed to send news item to {channel_id} ('{title[:50]}...'): {e}")
            return False
        except Exception as e: # Ловим другие возможные ошибки
             logger.error(f"Unexpected error sending news item to {channel_id} ('{title[:50]}...'): {e}", exc_info=True)
             return False

# Создаем экземпляр сервиса для импорта в другие модули
news_service = NewsService()