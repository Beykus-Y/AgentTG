# tools/basic_tools.py

import logging
import time
import asyncio
from typing import Dict, List, Optional, Tuple, Any

# Зависимости для парсинга чартов
try:
    import requests
    import aiohttp # Для асинхронного парсера (если будем использовать)
    from bs4 import BeautifulSoup
except ImportError:
    requests = None # type: ignore
    aiohttp = None # type: ignore
    BeautifulSoup = None # type: ignore
    logging.error("Missing libraries for music chart parsing: 'requests', 'aiohttp', 'beautifulsoup4'. Install them.")

logger = logging.getLogger(__name__)

# --- Имитация API Погоды (Асинхронная) ---
async def get_current_weather(location: str, unit: str = "celsius") -> Dict[str, Any]:
    """
    Получает текущую погоду для указанного местоположения (имитация).
    Возвращает словарь со статусом и данными о погоде или ошибкой.
    """
    tool_name = "get_current_weather"
    logger.info(f"--- Tool Call: {tool_name}(location='{location}', unit='{unit}') ---")
    if not location or not isinstance(location, str):
        return {"status": "error", "message": "Invalid location provided."}
    if unit not in ["celsius", "fahrenheit"]:
        logger.warning(f"{tool_name}: Invalid unit '{unit}'. Defaulting to celsius.")
        unit = "celsius"

    # Убираем имитацию задержки, т.к. функция async
    # await asyncio.sleep(0.1)
    location_lower = location.lower()
    weather_data = {}
    # Используем более реалистичные (хотя и вымышленные) данные
    if "tokyo" in location_lower:
        weather_data = {"location": "Tokyo, Japan", "temperature": "18", "unit": unit, "description": "Partly cloudy", "humidity": "65%"}
    elif "san francisco" in location_lower:
        weather_data = {"location": "San Francisco, CA, USA", "temperature": "16", "unit": unit, "description": "Sunny", "humidity": "70%"}
    elif "paris" in location_lower:
        weather_data = {"location": "Paris, France", "temperature": "14", "unit": unit, "description": "Light rain", "humidity": "80%"}
    elif "moscow" in location_lower:
         weather_data = {"location": "Moscow, Russia", "temperature": "10", "unit": unit, "description": "Cloudy", "humidity": "75%"}
    else:
        weather_data = {"location": location, "temperature": "unknown", "unit": unit, "description": "No data"}
        return {"status": "not_found", "message": f"Weather data not found for location '{location}'. Please specify city and country/region.", "data": weather_data}

    logger.debug(f"Weather data for '{location}': {weather_data}")
    return {"status": "success", "data": weather_data, "message": "Weather data retrieved."}


# --- Имитация API Акций (Асинхронная) ---
async def get_stock_price(ticker_symbol: str) -> Dict[str, Any]:
    """
    Получает текущую цену акции для указанного тикера (имитация).
    Возвращает словарь со статусом и данными акции или ошибкой.
    """
    tool_name = "get_stock_price"
    logger.info(f"--- Tool Call: {tool_name}(ticker_symbol='{ticker_symbol}') ---")
    if not ticker_symbol or not isinstance(ticker_symbol, str):
        return {"status": "error", "message": "Invalid ticker symbol provided."}

    # await asyncio.sleep(0.1) # Убираем имитацию задержки
    symbol = ticker_symbol.upper()
    stock_data = {}
    # Используем более реалистичные (вымышленные) данные
    if symbol == "GOOGL":
        stock_data = {"ticker": symbol, "company_name": "Alphabet Inc.", "price": "178.20", "currency": "USD", "change_percent": "+1.55%"}
        return {"status": "success", "data": stock_data, "message": "Stock price retrieved."}
    elif symbol == "AAPL":
        stock_data = {"ticker": symbol, "company_name": "Apple Inc.", "price": "191.50", "currency": "USD", "change_percent": "-0.28%"}
        return {"status": "success", "data": stock_data, "message": "Stock price retrieved."}
    elif symbol == "MSFT":
         stock_data = {"ticker": symbol, "company_name": "Microsoft Corp.", "price": "425.80", "currency": "USD", "change_percent": "+0.90%"}
         return {"status": "success", "data": stock_data, "message": "Stock price retrieved."}
    else:
        stock_data = {"ticker": symbol, "price": "unknown", "currency": "N/A", "change_percent": "N/A"}
        return {"status": "not_found", "message": f"Stock data not found for ticker symbol '{symbol}'. Please provide a valid ticker.", "data": stock_data}


# --- Парсер Музыкальных Чартов (Асинхронный) ---
async def _parse_yandex_chart_async(limit: int = 10) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Асинхронная функция парсинга чарта Яндекс.Музыки."""
    if not aiohttp or not BeautifulSoup:
         return None, "Missing required libraries: aiohttp, beautifulsoup4"

    url = "https://music.yandex.ru/chart"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
    }
    logger.debug(f"Fetching Yandex Music chart async from {url}")

    try:
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url) as response:
                response.raise_for_status() # Проверка HTTP ошибок
                html_content = await response.text()
                logger.debug(f"Yandex Music chart response status: {response.status}")

        # Парсинг HTML (синхронный, но быстрый)
        soup = BeautifulSoup(html_content, 'lxml')
        tracks: List[Dict[str, Any]] = []

        # Ищем контейнеры треков
        track_elements = soup.select('.d-track[data-item-id]')
        if not track_elements:
             logger.warning("Could not find track elements using '.d-track[data-item-id]' selector.")
             return None, "Could not find track elements on the Yandex Music chart page."

        logger.debug(f"Found {len(track_elements)} potential track elements.")
        for i, track_el in enumerate(track_elements):
            if len(tracks) >= limit: break
            try:
                title_el = track_el.select_one('.d-track__name a.d-track__title')
                artists_els = track_el.select('.d-track__artists a')

                if not title_el or not artists_els:
                     logger.warning(f"Skipping track element {i+1}: missing title or artists.")
                     continue

                title = title_el.text.strip()
                artists = ', '.join([a.text.strip() for a in artists_els])
                track_id = track_el.get('data-item-id')
                track_url = f"https://music.yandex.ru/track/{track_id}" if track_id else "N/A"

                tracks.append({
                    'position': len(tracks) + 1, # Позиция в чарте
                    'title': title,
                    'artist': artists,
                    'url': track_url
                })
            except Exception as parse_err:
                logger.warning(f"Error parsing a Yandex track element {i+1}: {parse_err}", exc_info=False)
                continue

        if not tracks:
            return None, "Failed to parse any tracks from the page content."

        logger.info(f"Successfully parsed {len(tracks)} tracks from Yandex Music chart async.")
        return tracks, None

    except aiohttp.ClientError as req_err:
        logger.error(f"Network error fetching Yandex Music chart async: {req_err}", exc_info=True)
        return None, f"Network error: {req_err}"
    except asyncio.TimeoutError:
         logger.error(f"Timeout error fetching Yandex Music chart async from {url}")
         return None, "Timeout error during chart request."
    except Exception as e:
        logger.error(f"Unexpected error parsing Yandex Music chart async: {e}", exc_info=True)
        return None, f"Unexpected parsing error: {e}"

async def get_music_charts(source: str = "yandex", limit: int = 10) -> Dict[str, Any]:
    """
    Асинхронно получает топ музыкальных треков из указанного источника.
    Пока поддерживает только 'yandex'.
    """
    tool_name = "get_music_charts"
    logger.info(f"--- Tool Call: {tool_name}(source='{source}', limit={limit}) ---")

    source_lower = source.lower()
    if source_lower != "yandex":
        return {"status": "error", "message": f"Source '{source}' not supported. Only 'yandex' is available."}
    if not isinstance(limit, int) or limit <= 0 or limit > 50:
        logger.warning(f"Invalid limit '{limit}'. Using default 10.")
        limit = 10

    tracks, error_msg = await _parse_yandex_chart_async(limit)

    if error_msg:
        return {"status": "error", "message": f"Failed to get chart data: {error_msg}"}
    elif tracks:
        return {
            "status": "success",
            "chart_source": "Yandex Music",
            "top_tracks": tracks, # Список словарей
            "message": f"Successfully retrieved top {len(tracks)} tracks from Yandex Music."
        }
    else:
        # Если не было ошибки, но и треков нет (маловероятно)
        return {"status": "error", "message": "Failed to get chart data (unknown parsing error or no tracks found)."}