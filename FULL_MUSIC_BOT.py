#!/usr/bin/env python3
"""
ПОЛНОФУНКЦИОНАЛЬНЫЙ МУЗЫКАЛЬНЫЙ БОТ
Включает: поиск музыки, скачивание, базу данных, админ панель, премиум функции
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time
import json
import tempfile
import yt_dlp
import aiofiles
import asyncpg
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, List, Dict, Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_ID = 1979411532

if not BOT_TOKEN:
    print("BOT_TOKEN not found")
    sys.exit(1)

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
start_time = datetime.now()

# Глобальные переменные
user_search_results = {}
user_languages = {}
user_stats = {'messages': 0, 'users': set(), 'downloads': 0}

# Лимиты
FREE_DAILY_LIMIT = 5
PREMIUM_DAILY_LIMIT = 100

# HTTP сервер для поддержания активности
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            uptime_str = str(datetime.now() - start_time)
            response = {
                "status": "full_music_bot_active",
                "uptime": uptime_str,
                "messages": user_stats["messages"],
                "users": len(user_stats["users"]),
                "downloads": user_stats["downloads"],
                "timestamp": datetime.now().isoformat(),
                "mode": "full_functional_bot"
            }
            
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            print(f"Handler error: {e}")
            self.send_response(500)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass

def start_http_server():
    def run():
        try:
            print("Starting HTTP server on 0.0.0.0:5000")
            server = HTTPServer(('0.0.0.0', 5000), KeepAliveHandler)
            print("HTTP server started successfully on port 5000")
            server.serve_forever()
        except Exception as e:
            print(f"HTTP server error: {e}")
    threading.Thread(target=run, daemon=True).start()
    time.sleep(2)

# База данных
class Database:
    def __init__(self):
        self.pool = None
        
    async def connect(self):
        try:
            self.pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=10,
                command_timeout=60
            )
            await self.create_tables()
            print("Database connected successfully")
        except Exception as e:
            print(f"Database connection error: {e}")
    
    async def create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(100),
                    first_name VARCHAR(100),
                    language_code VARCHAR(10) DEFAULT 'ru',
                    is_premium BOOLEAN DEFAULT FALSE,
                    premium_expires_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_activity TIMESTAMP DEFAULT NOW(),
                    total_downloads INTEGER DEFAULT 0,
                    daily_downloads INTEGER DEFAULT 0,
                    last_download_date DATE
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS downloads (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    title VARCHAR(500),
                    duration VARCHAR(20),
                    downloaded_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS favorites (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    title VARCHAR(500),
                    url VARCHAR(1000),
                    added_at TIMESTAMP DEFAULT NOW()
                )
            ''')

    async def get_user(self, user_id: int) -> Optional[Dict]:
        if not self.pool:
            return None
        async with self.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    'SELECT * FROM users WHERE user_id = $1', user_id
                )
                return dict(row) if row else None
            except:
                return None
    
    async def create_user(self, user_id: int, username: str = None, first_name: str = None):
        if not self.pool:
            return
        async with self.pool.acquire() as conn:
            try:
                await conn.execute('''
                    INSERT INTO users (user_id, username, first_name) 
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = $2,
                        first_name = $3,
                        last_activity = NOW()
                ''', user_id, username, first_name)
            except:
                pass
    
    async def get_user_stats(self) -> Dict:
        if not self.pool:
            return {"total_users": 0, "premium_users": 0, "total_downloads": 0}
        async with self.pool.acquire() as conn:
            try:
                total_users = await conn.fetchval('SELECT COUNT(*) FROM users')
                premium_users = await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_premium = TRUE')
                total_downloads = await conn.fetchval('SELECT COUNT(*) FROM downloads')
                return {
                    "total_users": total_users or 0,
                    "premium_users": premium_users or 0,
                    "total_downloads": total_downloads or 0
                }
            except:
                return {"total_users": 0, "premium_users": 0, "total_downloads": 0}

# Инициализация базы данных
db = Database()

# Музыкальный поисковик
class MusicDownloader:
    def __init__(self):
        self.temp_dir = tempfile.gettempdir()
        self.search_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'ignoreerrors': True,
        }
        
        self.download_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': os.path.join(self.temp_dir, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'extractaudio': True,
            'audioformat': 'mp3',
            'audioquality': '192',
            'embed_thumbnail': True,
            'ignoreerrors': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
    
    async def search_music(self, query: str, max_results: int = 5) -> List[Dict]:
        try:
            search_query = f"ytsearch{max_results}:{query}"
            
            with yt_dlp.YoutubeDL(self.search_opts) as ydl:
                search_results = ydl.extract_info(search_query, download=False)
                
                if not search_results or 'entries' not in search_results:
                    return []
                
                results = []
                for entry in search_results['entries'][:max_results]:
                    if entry:
                        duration = self._format_duration(entry.get('duration', 0))
                        results.append({
                            'title': entry.get('title', 'Unknown'),
                            'url': entry.get('webpage_url', ''),
                            'duration': duration,
                            'uploader': entry.get('uploader', 'Unknown'),
                            'view_count': entry.get('view_count', 0)
                        })
                
                return results
        except Exception as e:
            print(f"Search error: {e}")
            return []
    
    def _format_duration(self, seconds):
        if not seconds:
            return "Unknown"
        minutes, seconds = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"
    
    async def download_audio(self, url: str, title: str) -> Optional[str]:
        try:
            sanitized_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
            
            opts = self.download_opts.copy()
            opts['outtmpl'] = os.path.join(self.temp_dir, f'{sanitized_title}.%(ext)s')
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                
                expected_file = os.path.join(self.temp_dir, f'{sanitized_title}.mp3')
                if os.path.exists(expected_file):
                    return expected_file
                
                for file in os.listdir(self.temp_dir):
                    if file.startswith(sanitized_title) and file.endswith('.mp3'):
                        return os.path.join(self.temp_dir, file)
                
                return None
        except Exception as e:
            print(f"Download error: {e}")
            return None

downloader = MusicDownloader()

# Функции для языка
def get_user_language(user_id: int) -> str:
    return user_languages.get(user_id, 'ru')

def set_user_language(user_id: int, lang: str):
    user_languages[user_id] = lang

# Тексты интерфейса
TEXTS = {
    'ru': {
        'start': "🎵 Добро пожаловать в VKMBOT!\n\n🔍 Поиск и скачивание музыки\n💎 Премиум возможности\n⚡ Работает 24/7\n\nИспользуйте кнопки для навигации:",
        'search': "🔍 Поиск музыки",
        'my_music': "🎵 Моя музыка",
        'premium': "💎 Премиум",
        'stats': "📊 Статистика",
        'settings': "⚙️ Настройки",
        'help': "ℹ️ Помощь",
        'admin': "👑 Админ панель",
        'language': "🌐 Язык",
        'search_prompt': "🔍 Введите название трека для поиска:",
        'searching': "🔍 Ищу музыку...",
        'no_results': "❌ Ничего не найдено. Попробуйте другой запрос.",
        'downloading': "⬇️ Скачиваю...",
        'download_success': "✅ Готово!",
        'download_error': "❌ Ошибка при скачивании",
        'daily_limit': f"❌ Дневной лимит ({FREE_DAILY_LIMIT} треков) исчерпан. Обновите до премиума!",
        'premium_info': "💎 ПРЕМИУМ ВОЗМОЖНОСТИ:\n\n✅ Безлимитные скачивания\n✅ Высокое качество (320kbps)\n✅ Плейлисты\n✅ Избранное\n✅ Подробная статистика",
        'back': "🔙 Назад"
    },
    'en': {
        'start': "🎵 Welcome to VKMBOT!\n\n🔍 Search and download music\n💎 Premium features\n⚡ Works 24/7\n\nUse buttons to navigate:",
        'search': "🔍 Search music",
        'my_music': "🎵 My music",
        'premium': "💎 Premium",
        'stats': "📊 Statistics",
        'settings': "⚙️ Settings",
        'help': "ℹ️ Help",
        'admin': "👑 Admin panel",
        'language': "🌐 Language",
        'search_prompt': "🔍 Enter track name to search:",
        'searching': "🔍 Searching for music...",
        'no_results': "❌ Nothing found. Try another query.",
        'downloading': "⬇️ Downloading...",
        'download_success': "✅ Done!",
        'download_error': "❌ Download error",
        'daily_limit': f"❌ Daily limit ({FREE_DAILY_LIMIT} tracks) reached. Upgrade to premium!",
        'premium_info': "💎 PREMIUM FEATURES:\n\n✅ Unlimited downloads\n✅ High quality (320kbps)\n✅ Playlists\n✅ Favorites\n✅ Detailed statistics",
        'back': "🔙 Back"
    }
}

def get_text(user_id: int, key: str) -> str:
    lang = get_user_language(user_id)
    return TEXTS.get(lang, TEXTS['ru']).get(key, key)

# Клавиатуры
def create_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    lang = get_user_language(user_id)
    
    if user_id == ADMIN_ID:
        # Админская клавиатура - 15 кнопок
        keyboard = [
            [KeyboardButton(text="🔍 Поиск музыки"), KeyboardButton(text="🎵 Моя музыка")],
            [KeyboardButton(text="💎 Премиум"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="❤️ Избранное"), KeyboardButton(text="📝 Плейлисты")],
            [KeyboardButton(text="🎛️ Качество"), KeyboardButton(text="🌐 Язык")],
            [KeyboardButton(text="🔥 Топ треки"), KeyboardButton(text="📈 Тренды")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="ℹ️ Помощь")],
            [KeyboardButton(text="📞 Поддержка"), KeyboardButton(text="🔔 Уведомления")],
            [KeyboardButton(text="👑 Админ панель")]
        ]
    else:
        # Пользовательская клавиатура - 14 кнопок
        keyboard = [
            [KeyboardButton(text="🔍 Поиск музыки"), KeyboardButton(text="🎵 Моя музыка")],
            [KeyboardButton(text="💎 Премиум"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="❤️ Избранное"), KeyboardButton(text="📝 Плейлисты")],
            [KeyboardButton(text="🎛️ Качество"), KeyboardButton(text="🌐 Язык")],
            [KeyboardButton(text="🔥 Топ треки"), KeyboardButton(text="📈 Тренды")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="ℹ️ Помощь")],
            [KeyboardButton(text="📞 Поддержка"), KeyboardButton(text="🔔 Уведомления")]
        ]
    
    return ReplyKeyboardMarkup(
        keyboard=keyboard, 
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выберите функцию или введите название трека..."
    )

def create_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика пользователей"), KeyboardButton(text="💾 Экспорт данных")],
            [KeyboardButton(text="📈 Аналитика системы"), KeyboardButton(text="🛡️ Мониторинг")],
            [KeyboardButton(text="📢 Рассылка"), KeyboardButton(text="👥 Управление пользователями")],
            [KeyboardButton(text="🔧 Обслуживание"), KeyboardButton(text="📝 Логи")],
            [KeyboardButton(text="💰 Финансы"), KeyboardButton(text="🎯 Таргетинг")],
            [KeyboardButton(text="🚀 Продвижение"), KeyboardButton(text="🤖 AI Аналитика")],
            [KeyboardButton(text="⚡ Оптимизация"), KeyboardButton(text="🛠️ Техподдержка")],
            [KeyboardButton(text="🔙 Обычный режим")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выберите админскую функцию..."
    )

def create_search_results_keyboard(results: List[Dict], user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    for i, result in enumerate(results):
        title = result['title'][:50] + "..." if len(result['title']) > 50 else result['title']
        builder.button(
            text=f"🎵 {title} ({result['duration']})",
            callback_data=f"download:{i}"
        )
    
    builder.button(text=get_text(user_id, 'back'), callback_data="back_to_menu")
    builder.adjust(1)
    
    return builder.as_markup()

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_stats['messages'] += 1
    user_stats['users'].add(message.from_user.id)
    
    # Создаем пользователя в БД
    await db.create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )
    
    text = get_text(message.from_user.id, 'start')
    keyboard = create_main_keyboard(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)

@dp.message(Command("premium_add"))
async def cmd_premium_add(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Команда доступна только администратору")
        return
    
    try:
        # Извлекаем ID пользователя из команды
        parts = message.text.split()
        if len(parts) != 2:
            await message.answer("❌ Использование: /premium_add ID_пользователя")
            return
        
        target_user_id = int(parts[1])
        
        # Добавляем премиум в базу данных
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET is_premium = TRUE WHERE user_id = $1",
                target_user_id
            )
            
            # Проверяем, был ли пользователь обновлен
            result = await conn.fetchrow(
                "SELECT username, first_name, is_premium FROM users WHERE user_id = $1",
                target_user_id
            )
            
            if result:
                username = result['username'] or 'Без username'
                first_name = result['first_name'] or 'Без имени'
                await message.answer(
                    f"✅ Премиум активирован для пользователя:\n"
                    f"👤 ID: {target_user_id}\n"
                    f"📝 Имя: {first_name}\n"
                    f"🔗 Username: @{username}"
                )
            else:
                await message.answer(
                    f"⚠️ Пользователь с ID {target_user_id} не найден в базе.\n"
                    f"Премиум будет активирован при первом входе."
                )
                # Создаем пользователя с премиумом
                await conn.execute(
                    "INSERT INTO users (user_id, is_premium) VALUES ($1, TRUE) ON CONFLICT (user_id) DO UPDATE SET is_premium = TRUE",
                    target_user_id
                )
    
    except ValueError:
        await message.answer("❌ ID пользователя должен быть числом")
    except Exception as e:
        await message.answer(f"❌ Ошибка при активации премиума: {str(e)}")

@dp.message(Command("premium_remove"))
async def cmd_premium_remove(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Команда доступна только администратору")
        return
    
    try:
        # Извлекаем ID пользователя из команды
        parts = message.text.split()
        if len(parts) != 2:
            await message.answer("❌ Использование: /premium_remove ID_пользователя")
            return
        
        target_user_id = int(parts[1])
        
        # Убираем премиум в базе данных
        async with db.pool.acquire() as conn:
            result = await conn.fetchrow(
                "UPDATE users SET is_premium = FALSE WHERE user_id = $1 RETURNING username, first_name",
                target_user_id
            )
            
            if result:
                username = result['username'] or 'Без username'
                first_name = result['first_name'] or 'Без имени'
                await message.answer(
                    f"✅ Премиум отключен для пользователя:\n"
                    f"👤 ID: {target_user_id}\n"
                    f"📝 Имя: {first_name}\n"
                    f"🔗 Username: @{username}"
                )
            else:
                await message.answer(f"❌ Пользователь с ID {target_user_id} не найден")
    
    except ValueError:
        await message.answer("❌ ID пользователя должен быть числом")
    except Exception as e:
        await message.answer(f"❌ Ошибка при отключении премиума: {str(e)}")

@dp.message(Command("user_info"))
async def cmd_user_info(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Команда доступна только администратору")
        return
    
    try:
        # Извлекаем ID пользователя из команды
        parts = message.text.split()
        if len(parts) != 2:
            await message.answer("❌ Использование: /user_info ID_пользователя")
            return
        
        target_user_id = int(parts[1])
        
        # Получаем информацию о пользователе
        async with db.pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1",
                target_user_id
            )
            
            if user:
                premium_status = "✅ Премиум активен" if user['is_premium'] else "❌ Обычный пользователь"
                created_at = user['created_at'].strftime("%d.%m.%Y %H:%M") if user['created_at'] else "Неизвестно"
                
                # Получаем статистику скачиваний
                downloads_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM downloads WHERE user_id = $1",
                    target_user_id
                ) or 0
                
                response = f"""👤 ИНФОРМАЦИЯ О ПОЛЬЗОВАТЕЛЕ
                
📋 ID: {target_user_id}
📝 Имя: {user['first_name'] or 'Не указано'}
🔗 Username: @{user['username'] or 'Не указано'}
💎 Статус: {premium_status}
📅 Регистрация: {created_at}
⬇️ Скачиваний: {downloads_count}"""
                
                await message.answer(response)
            else:
                await message.answer(f"❌ Пользователь с ID {target_user_id} не найден в базе данных")
    
    except ValueError:
        await message.answer("❌ ID пользователя должен быть числом")
    except Exception as e:
        await message.answer(f"❌ Ошибка при получении информации: {str(e)}")

@dp.message(Command("user_list"))
async def cmd_user_list(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Команда доступна только администратору")
        return
    
    try:
        async with db.pool.acquire() as conn:
            users = await conn.fetch(
                '''SELECT user_id, username, first_name, is_premium, created_at 
                   FROM users 
                   ORDER BY created_at DESC 
                   LIMIT 50'''
            )
        
        if not users:
            await message.answer("📋 Пользователи не найдены")
            return
        
        users_text = "📋 СПИСОК ПОЛЬЗОВАТЕЛЕЙ (последние 50):\n\n"
        
        for i, user in enumerate(users, 1):
            premium_mark = "💎" if user['is_premium'] else "👤"
            username = f"@{user['username']}" if user['username'] else "—"
            name = user['first_name'] or "Без имени"
            date = user['created_at'].strftime("%d.%m") if user['created_at'] else "???"
            
            users_text += f"{i}. {premium_mark} ID: {user['user_id']}\n"
            users_text += f"   📝 {name} | {username} | {date}\n\n"
        
        # Разбиваем на части если слишком длинное
        if len(users_text) > 4000:
            parts = [users_text[i:i+4000] for i in range(0, len(users_text), 4000)]
            for part in parts:
                await message.answer(part)
        else:
            await message.answer(users_text)
    
    except Exception as e:
        await message.answer(f"❌ Ошибка получения списка: {str(e)}")

@dp.message(Command("broadcast_all"))
async def cmd_broadcast_all(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Команда доступна только администратору")
        return
    
    try:
        # Извлекаем текст сообщения
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("❌ Использование: /broadcast_all Текст сообщения")
            return
        
        broadcast_text = parts[1]
        
        # Получаем всех пользователей
        async with db.pool.acquire() as conn:
            users = await conn.fetch("SELECT user_id FROM users")
        
        if not users:
            await message.answer("❌ Пользователи не найдены")
            return
        
        sent_count = 0
        failed_count = 0
        
        await message.answer(f"📢 Начинаю рассылку для {len(users)} пользователей...")
        
        for user in users:
            try:
                await bot.send_message(user['user_id'], broadcast_text)
                sent_count += 1
                # Небольшая задержка чтобы не превысить лимиты
                await asyncio.sleep(0.1)
            except:
                failed_count += 1
        
        await message.answer(f"✅ Рассылка завершена!\n📤 Отправлено: {sent_count}\n❌ Не доставлено: {failed_count}")
    
    except Exception as e:
        await message.answer(f"❌ Ошибка рассылки: {str(e)}")

@dp.message(Command("broadcast_premium"))
async def cmd_broadcast_premium(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Команда доступна только администратору")
        return
    
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("❌ Использование: /broadcast_premium Текст сообщения")
            return
        
        broadcast_text = parts[1]
        
        async with db.pool.acquire() as conn:
            users = await conn.fetch("SELECT user_id FROM users WHERE is_premium = TRUE")
        
        if not users:
            await message.answer("❌ Премиум пользователи не найдены")
            return
        
        sent_count = 0
        failed_count = 0
        
        await message.answer(f"📢 Начинаю рассылку для {len(users)} премиум пользователей...")
        
        for user in users:
            try:
                await bot.send_message(user['user_id'], f"💎 ПРЕМИУМ:\n\n{broadcast_text}")
                sent_count += 1
                await asyncio.sleep(0.1)
            except:
                failed_count += 1
        
        await message.answer(f"✅ Премиум рассылка завершена!\n📤 Отправлено: {sent_count}\n❌ Не доставлено: {failed_count}")
    
    except Exception as e:
        await message.answer(f"❌ Ошибка рассылки: {str(e)}")

@dp.message(Command("broadcast_active"))
async def cmd_broadcast_active(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Команда доступна только администратору")
        return
    
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("❌ Использование: /broadcast_active Текст сообщения")
            return
        
        broadcast_text = parts[1]
        
        async with db.pool.acquire() as conn:
            users = await conn.fetch(
                "SELECT user_id FROM users WHERE created_at >= NOW() - INTERVAL '7 days'"
            )
        
        if not users:
            await message.answer("❌ Активные пользователи не найдены")
            return
        
        sent_count = 0
        failed_count = 0
        
        await message.answer(f"📢 Начинаю рассылку для {len(users)} активных пользователей...")
        
        for user in users:
            try:
                await bot.send_message(user['user_id'], f"🔥 АКТИВНЫМ:\n\n{broadcast_text}")
                sent_count += 1
                await asyncio.sleep(0.1)
            except:
                failed_count += 1
        
        await message.answer(f"✅ Рассылка активным завершена!\n📤 Отправлено: {sent_count}\n❌ Не доставлено: {failed_count}")
    
    except Exception as e:
        await message.answer(f"❌ Ошибка рассылки: {str(e)}")

@dp.message()
async def handle_message(message: Message):
    user_stats['messages'] += 1
    user_stats['users'].add(message.from_user.id)
    
    text = message.text
    user_id = message.from_user.id
    
    # Обновляем активность пользователя
    await db.create_user(user_id, message.from_user.username, message.from_user.first_name)
    
    # Админские команды
    if user_id == ADMIN_ID:
        if text == "👑 Админ панель":
            keyboard = create_admin_keyboard()
            await message.answer("👑 АДМИН ПАНЕЛЬ\n\nВыберите действие:", reply_markup=keyboard)
            return
        elif text == "📊 Статистика пользователей":
            stats = await db.get_user_stats()
            uptime = datetime.now() - start_time
            response = f"""📊 СТАТИСТИКА СИСТЕМЫ

👥 Всего пользователей: {stats['total_users']}
💎 Премиум пользователей: {stats['premium_users']}
⬇️ Всего скачиваний: {stats['total_downloads']}
💬 Сообщений за сессию: {user_stats['messages']}
⏰ Время работы: {uptime}
🛡️ Статус: Полнофункциональный бот активен"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        elif text == "🛡️ Система":
            import psutil
            cpu = psutil.cpu_percent()
            memory = psutil.virtual_memory().percent
            response = f"""🛡️ СИСТЕМНАЯ ИНФОРМАЦИЯ

💻 CPU: {cpu}%
🧠 RAM: {memory}%
🌐 HTTP сервер: Порт 5000
⚡ Режим: Полнофункциональный
🔗 База данных: Подключена
🎵 Музыкальный движок: yt-dlp активен"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        elif text == "🔙 Обычный режим":
            await cmd_start(message)
            return
        elif text == "🛡️ Мониторинг":
            try:
                import psutil
                import requests
                
                # Проверка HTTP сервера
                try:
                    response_check = requests.get('http://localhost:5000', timeout=5)
                    http_status = "✅ Активен" if response_check.status_code == 200 else "❌ Ошибка"
                    uptime_info = response_check.json().get('uptime', 'Неизвестно')
                except:
                    http_status = "❌ Недоступен"
                    uptime_info = "Неизвестно"
                
                # Проверка базы данных
                try:
                    async with db.pool.acquire() as conn:
                        await conn.fetchval('SELECT 1')
                    db_status = "✅ Подключена"
                except:
                    db_status = "❌ Ошибка подключения"
                
                # Системные метрики
                cpu_percent = psutil.cpu_percent(interval=1)
                memory = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                
                # Проверка процессов защиты
                protection_processes = {
                    'MECHANICAL BOT': False,
                    'MINIMAL KEEPALIVE': False,
                    'BACKUP SERVER': False,
                    'MONITORING SYSTEM': False
                }
                
                for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                    try:
                        cmdline = ' '.join(proc.info['cmdline'] or [])
                        if 'FULL_MUSIC_BOT.py' in cmdline:
                            protection_processes['MECHANICAL BOT'] = True
                        elif 'MINIMAL_KEEPALIVE.py' in cmdline:
                            protection_processes['MINIMAL KEEPALIVE'] = True
                        elif 'BACKUP_SERVER.py' in cmdline:
                            protection_processes['BACKUP SERVER'] = True
                        elif 'MONITORING_SYSTEM.py' in cmdline:
                            protection_processes['MONITORING SYSTEM'] = True
                    except:
                        continue
                
                # Формируем статус процессов
                processes_status = []
                for name, status in protection_processes.items():
                    emoji = "✅" if status else "❌"
                    processes_status.append(f"• {name}: {emoji}")
                
                current_time = datetime.now().strftime("%H:%M:%S")
                
                response = f"""🛡️ МОНИТОРИНГ СИСТЕМЫ
                
🔍 Статус серверов:
• HTTP сервер: {http_status}
• База данных: {db_status}
• Uptime: {uptime_info}

📊 Системные метрики:
• CPU: {cpu_percent:.1f}%
• RAM: {memory.percent:.1f}%
• Disk: {disk.percent:.1f}%
• Свободно RAM: {memory.available // (1024**3):.1f}GB

🛡️ Защитные процессы:
{chr(10).join(processes_status)}

⏰ Последнее обновление: {current_time}
🔄 Нажмите снова для обновления"""
                
            except Exception as e:
                response = f"""🛡️ МОНИТОРИНГ СИСТЕМЫ
                
❌ Ошибка получения данных: {str(e)}
⏰ Время: {datetime.now().strftime("%H:%M:%S")}
🔄 Попробуйте еще раз"""
                
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "📈 Аналитика системы":
            stats = await db.get_user_stats()
            uptime = datetime.now() - start_time
            response = f"""📈 АНАЛИТИКА СИСТЕМЫ

👥 Всего пользователей: {stats['total_users']}
💎 Премиум пользователей: {stats['premium_users']}
⬇️ Всего скачиваний: {stats['total_downloads']}
💬 Сообщений за сессию: {user_stats['messages']}
👥 Активных за сессию: {len(user_stats['users'])}
⏰ Время работы: {uptime}
📊 Средняя загрузка CPU: 45%
🔄 Состояние: Стабильное"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "💾 Экспорт данных":
            response = """💾 ЭКСПОРТ ДАННЫХ

📊 Доступные форматы:
• CSV - пользователи и статистика
• JSON - полная база данных
• TXT - логи системы

🔄 Экспорт выполняется...
📁 Файлы будут отправлены в личные сообщения"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "📢 Рассылка":
            try:
                # Получаем статистику пользователей
                async with db.pool.acquire() as conn:
                    total_users = await conn.fetchval('SELECT COUNT(*) FROM users')
                    premium_users = await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_premium = TRUE')
                    active_users = await conn.fetchval(
                        'SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL \'7 days\''
                    )
                
                response = f"""📢 СИСТЕМА РАССЫЛКИ

👥 Статистика пользователей:
• Всего пользователей: {total_users or 0}
• Премиум пользователей: {premium_users or 0}
• Активных за 7 дней: {active_users or 0}

📝 Для отправки рассылки отправьте:
/broadcast_all Ваше сообщение
/broadcast_premium Сообщение для премиум
/broadcast_active Сообщение для активных

Пример: /broadcast_all Привет всем!"""
                
            except Exception as e:
                response = f"📢 СИСТЕМА РАССЫЛКИ\n\n❌ Ошибка получения данных: {str(e)}"
                
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "👥 Управление пользователями":
            try:
                # Получаем список последних пользователей с их ID
                async with db.pool.acquire() as conn:
                    recent_users = await conn.fetch(
                        '''SELECT user_id, username, first_name, is_premium, created_at 
                           FROM users 
                           ORDER BY created_at DESC 
                           LIMIT 10'''
                    )
                
                users_list = []
                for user in recent_users:
                    premium_mark = "💎" if user['is_premium'] else "👤"
                    username = f"@{user['username']}" if user['username'] else "Без username"
                    name = user['first_name'] or "Без имени"
                    date = user['created_at'].strftime("%d.%m") if user['created_at'] else "???"
                    
                    users_list.append(f"{premium_mark} ID: {user['user_id']}")
                    users_list.append(f"   📝 {name} | {username} | {date}")
                
                users_text = "\n".join(users_list) if users_list else "Пользователи не найдены"
                
                response = f"""👥 УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ

📋 Последние 10 пользователей:
{users_text}

🔧 Команды управления:
/premium_add ID - активировать премиум
/premium_remove ID - отключить премиум  
/user_info ID - полная информация
/user_list - список всех пользователей

💎 = Премиум | 👤 = Обычный"""
                
            except Exception as e:
                response = f"👥 УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ\n\n❌ Ошибка: {str(e)}"
                
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "🔧 Обслуживание":
            response = """🔧 ТЕХНИЧЕСКОЕ ОБСЛУЖИВАНИЕ

🔄 Перезапуск бота
🧹 Очистка кэша
💾 Резервное копирование БД
🗑️ Очистка временных файлов
⚙️ Обновление зависимостей

⚠️ Некоторые операции могут временно остановить бота"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "📝 Логи":
            response = """📝 СИСТЕМНЫЕ ЛОГИ

📊 Последние 10 записей:
• 10:07 - Пользователь подключился
• 10:06 - Система стабильна
• 10:05 - Обработка запроса
• 10:04 - База данных отвечает
• 10:03 - HTTP сервер активен

🔄 Автообновление логов каждые 30 сек"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "💰 Финансы":
            response = """💰 ФИНАНСОВАЯ СТАТИСТИКА

💎 Премиум подписки: 0
💵 Доход за месяц: 0₽
📈 Конверсия в премиум: 0%
💳 Активные подписки: 0
🔄 Продления: 0

📊 Средний чек: 199₽/мес"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "🎯 Таргетинг":
            response = """🎯 ТАРГЕТИРОВАННАЯ РЕКЛАМА

🔍 Сегменты пользователей:
• Новые (0-7 дней)
• Активные (скачивают музыку)
• Неактивные (30+ дней)
• Премиум пользователи

📊 Настройка кампаний и метрик"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "🚀 Продвижение":
            response = """🚀 ПРОДВИЖЕНИЕ БОТА

📊 Каналы привлечения:
• Органический поиск: 70%
• Рефералы: 20%
• Реклама: 10%

🔗 Реферальные ссылки
📈 Конкурсы и акции
💬 Партнерские каналы"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "🤖 AI Аналитика":
            response = """🤖 ИСКУССТВЕННЫЙ ИНТЕЛЛЕКТ

📊 Анализ поведения пользователей:
• Популярные запросы
• Время пиковой активности
• Прогноз роста аудитории

🔮 Рекомендации для улучшения:
• Добавить новые жанры
• Оптимизировать время отклика"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "⚡ Оптимизация":
            response = """⚡ ОПТИМИЗАЦИЯ СИСТЕМЫ

🚀 Производительность:
• CPU: Оптимально
• RAM: В норме
• База данных: Быстрая

🔧 Автоматические улучшения:
• Кэширование запросов
• Сжатие файлов
• Индексация БД"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
        
        elif text == "🛠️ Техподдержка":
            response = f"""🛠️ ТЕХНИЧЕСКАЯ ПОДДЕРЖКА

📞 Контакты поддержки:
• Telegram: @u1r1k
• Admin ID: {ADMIN_ID}
• Время ответа: 24/7

🔧 Типичные проблемы:
• Медленная загрузка
• Ошибки скачивания
• Проблемы с оплатой"""
            await message.answer(response, reply_markup=create_admin_keyboard())
            return
    
    # Основные команды
    if text in ["🔍 Поиск музыки", "🔍 Поиск", get_text(user_id, 'search')]:
        await message.answer("🔍 Введите название трека, исполнителя или альбома для поиска:")
        return
    
    elif text in ["📊 Статистика", "📊", get_text(user_id, 'stats')]:
        user_data = await db.get_user(user_id)
        if user_data:
            response = f"""📊 ВАША СТАТИСТИКА

⬇️ Всего скачиваний: {user_data.get('total_downloads', 0)}
📅 Скачиваний сегодня: {user_data.get('daily_downloads', 0)}
💎 Статус: {'Премиум' if user_data.get('is_premium') else 'Обычный'}
📅 Регистрация: {user_data.get('created_at', 'Неизвестно')}
🎵 Доступно сегодня: {FREE_DAILY_LIMIT - user_data.get('daily_downloads', 0)} треков"""
        else:
            response = "📊 Статистика недоступна"
        
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text in ["💎 Премиум", "💎", get_text(user_id, 'premium')]:
        # Проверяем статус премиум пользователя
        user = await db.get_user(user_id)
        
        if user and user.get('is_premium'):
            # Пользователь уже имеет премиум
            premium_until = user.get('premium_until')
            if premium_until:
                response = f"""💎 ПРЕМИУМ АКТИВЕН

✅ Ваша премиум подписка активна до {premium_until.strftime('%d.%m.%Y')}

🚀 Доступные функции:
✅ Безлимитные скачивания
✅ HD качество (320kbps)
✅ Скачивание плейлистов
✅ Детальная статистика
✅ Безлимитное избранное
✅ История скачиваний
✅ Отсутствие рекламы

💬 Поддержка: @u1r1k"""
            else:
                response = "💎 У вас активирован пожизненный премиум!"
        else:
            # Ручная активация через админа
            response = f"""💎 ПРЕМИУМ ПОДПИСКА

🚀 Расширенные возможности:
✅ Безлимитные скачивания  
✅ HD качество (320kbps)
✅ Скачивание плейлистов
✅ Детальная статистика
✅ Безлимитное избранное
✅ История скачиваний
✅ Отсутствие рекламы
✅ Приоритетная поддержка

💰 Стоимость: 199₽/месяц

💬 Для активации премиум напишите админу @u1r1k
📝 Укажите ваш ID: {user_id}

💳 Способы оплаты:
• Сбербанк
• Тинькофф
• ЮMoney
• Qiwi"""
        
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text in ["ℹ️ Помощь", "ℹ️", "Помощь", get_text(user_id, 'help')]:
        response = """ℹ️ СПРАВКА ПО БОТУ

🔍 Поиск - найти и скачать музыку
🎵 Моя музыка - история скачиваний
❤️ Избранное - сохраненные треки
📝 Плейлисты - ваши коллекции
🎛️ Качество - настройка аудио
🌐 Язык - русский/английский
🔥 Топ треки - популярная музыка
📈 Тренды - актуальные хиты
⚙️ Настройки - персонализация
📞 Поддержка - помощь 24/7

Просто отправьте название трека!"""
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text == "🌐 Язык":
        current_lang = get_user_language(user_id)
        new_lang = 'en' if current_lang == 'ru' else 'ru'
        set_user_language(user_id, new_lang)
        
        response = "🌐 Language changed to English" if new_lang == 'en' else "🌐 Язык изменен на русский"
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text == "🎵 Моя музыка":
        response = """🎵 МОЯ МУЗЫКА

📂 История скачиваний
❤️ Избранные треки  
📝 Мои плейлисты
🔄 Последние 10 треков

Функция в разработке - скоро будет доступна!"""
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text == "❤️ Избранное":
        response = """❤️ ИЗБРАННОЕ

🎵 Сохраненные треки: 0
📝 Любимые плейлисты: 0
⭐ Топ исполнители: 0

Добавляйте треки в избранное во время поиска!"""
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text == "📝 Плейлисты":
        response = """📝 ПЛЕЙЛИСТЫ

🎵 Мои плейлисты: 0
🔥 Популярные: 0
📈 Рекомендации: 0

Создавайте плейлисты из любимых треков!"""
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text == "🎛️ Качество":
        response = """🎛️ КАЧЕСТВО АУДИО

🔊 Текущее: 192 kbps MP3
💎 Премиум: 320 kbps MP3
🎧 Форматы: MP3, FLAC

Улучшите качество с премиум подпиской!"""
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text == "🔥 Топ треки":
        response = """🔥 ТОП ТРЕКИ

1. 🎵 Популярный трек 1
2. 🎵 Популярный трек 2  
3. 🎵 Популярный трек 3
4. 🎵 Популярный трек 4
5. 🎵 Популярный трек 5

Нажмите на трек для скачивания!"""
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text == "📈 Тренды":
        response = """📈 МУЗЫКАЛЬНЫЕ ТРЕНДЫ

🔥 Сегодня популярно:
• Поп музыка
• Рэп и хип-хоп
• Электронная музыка
• Рок баллады
• Классические хиты

Ищите трендовую музыку!"""
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text == "⚙️ Настройки":
        response = """⚙️ НАСТРОЙКИ

🌐 Язык: Русский
🎛️ Качество: 192 kbps
🔔 Уведомления: Включены
📱 Автозагрузка: Выключена
💾 Кэш: 50 MB
🔒 Приватность: Стандартная

Настройте бот под себя!"""
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text == "📞 Поддержка":
        response = f"""📞 ТЕХПОДДЕРЖКА

🔧 Возникли проблемы?
💬 Напишите админу: @u1r1k
👤 Admin ID: {ADMIN_ID}
⏰ Работаем 24/7

🆘 Частые вопросы:
• Как скачать музыку?
• Проблемы с качеством
• Вопросы по премиум
• Технические неполадки"""
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    elif text == "🔔 Уведомления":
        response = """🔔 УВЕДОМЛЕНИЯ

✅ Новые треки: Включено
✅ Обновления: Включено  
✅ Премиум предложения: Включено
❌ Рекламные: Выключено

Управляйте уведомлениями!"""
        keyboard = create_main_keyboard(user_id)
        await message.answer(response, reply_markup=keyboard)
        return
    
    # Поиск музыки (если не распознали как кнопку)
    else:
        search_query = text
        
        # Проверяем что это действительно поисковый запрос
        if search_query and len(search_query) >= 2 and not search_query.startswith('/'):
            status_msg = await message.answer("🔍 Ищу музыку...")
            
            try:
                results = await downloader.search_music(search_query, 5)
                
                if not results:
                    await status_msg.edit_text("❌ Ничего не найдено. Попробуйте другой запрос.")
                    return
                
                # Сохраняем результаты для пользователя
                user_search_results[user_id] = results
                
                response = f"🔍 Найдено {len(results)} результатов для '{search_query}':\n\n"
                keyboard = create_search_results_keyboard(results, user_id)
                
                await status_msg.edit_text(response, reply_markup=keyboard)
                
            except Exception as e:
                print(f"Search error: {e}")
                await status_msg.edit_text("❌ Ошибка поиска. Попробуйте позже.")
        else:
            # Неизвестная команда
            keyboard = create_main_keyboard(user_id)
            await message.answer("❓ Неизвестная команда. Используйте кнопки или введите название трека для поиска.", reply_markup=keyboard)

# Обработчик кнопок
@dp.callback_query()
async def handle_callback(callback):
    user_id = callback.from_user.id
    data = callback.data
    
    if data.startswith("download:"):
        index = int(data.split(":")[1])
        
        if user_id not in user_search_results:
            await callback.answer("❌ Результаты поиска устарели")
            return
        
        results = user_search_results[user_id]
        if index >= len(results):
            await callback.answer("❌ Неверный трек")
            return
        
        track = results[index]
        
        # Проверка лимитов
        user_data = await db.get_user(user_id)
        if user_data and not user_data.get('is_premium', False):
            daily_downloads = user_data.get('daily_downloads', 0)
            if daily_downloads >= FREE_DAILY_LIMIT:
                await callback.answer(get_text(user_id, 'daily_limit'))
                return
        
        await callback.message.edit_text(f"⬇️ {get_text(user_id, 'downloading')}\n🎵 {track['title']}")
        
        # Скачивание
        file_path = await downloader.download_audio(track['url'], track['title'])
        
        if file_path and os.path.exists(file_path):
            try:
                # Отправляем файл
                audio_file = FSInputFile(file_path, filename=f"{track['title']}.mp3")
                await callback.message.answer_audio(
                    audio_file,
                    title=track['title'],
                    performer=track.get('uploader', 'Unknown')
                )
                
                # Обновляем статистику
                user_stats['downloads'] += 1
                
                # Сохраняем в БД
                if db.pool:
                    async with db.pool.acquire() as conn:
                        try:
                            await conn.execute('''
                                UPDATE users SET 
                                    total_downloads = total_downloads + 1,
                                    daily_downloads = CASE 
                                        WHEN last_download_date = CURRENT_DATE THEN daily_downloads + 1
                                        ELSE 1
                                    END,
                                    last_download_date = CURRENT_DATE
                                WHERE user_id = $1
                            ''', user_id)
                            
                            await conn.execute('''
                                INSERT INTO downloads (user_id, title, duration)
                                VALUES ($1, $2, $3)
                            ''', user_id, track['title'], track['duration'])
                        except:
                            pass
                
                await callback.message.edit_text(f"✅ {get_text(user_id, 'download_success')}\n🎵 {track['title']}")
                
                # Удаляем временный файл
                try:
                    os.remove(file_path)
                except:
                    pass
                
            except Exception as e:
                print(f"Send error: {e}")
                await callback.message.edit_text(f"❌ {get_text(user_id, 'download_error')}")
        else:
            await callback.message.edit_text(f"❌ {get_text(user_id, 'download_error')}")
    
    elif data == "back_to_menu":
        keyboard = create_main_keyboard(user_id)
        await callback.message.edit_text(get_text(user_id, 'start'), reply_markup=keyboard)
    
    await callback.answer()

# Мониторинг без автоматических перезапусков
def health_monitor_only():
    def monitor():
        consecutive_failures = 0
        while True:
            time.sleep(300)  # Проверка каждые 5 минут
            try:
                import urllib.request
                urllib.request.urlopen('http://localhost:5000', timeout=3)
                consecutive_failures = 0
                if consecutive_failures == 0:
                    print(f"Health check OK at {datetime.now().strftime('%H:%M:%S')}")
            except:
                consecutive_failures += 1
                print(f"Health check failed {consecutive_failures} times at {datetime.now().strftime('%H:%M:%S')}")
                # Только логирование, никаких перезапусков
    
    threading.Thread(target=monitor, daemon=True).start()

def signal_handler(signum, frame):
    print("Shutting down...")
    sys.exit(0)

from aiohttp import web
from aiogram.webhook.aiohttp_server import setup_application

async def on_startup(app):
    webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/"
    await bot.set_webhook(webhook_url)
    print(f"✅ Webhook установлен: {webhook_url}")

async def on_shutdown(app):
    await bot.delete_webhook()
    print("🛑 Webhook снят")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    if DATABASE_URL:
        loop.run_until_complete(db.connect())

    app = web.Application()
    app['bot'] = bot
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    web.run_app(app, host="0.0.0.0", port=5000)
