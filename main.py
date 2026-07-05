# main.py
import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, Optional, Set, List, Tuple
import os
from dotenv import load_dotenv
import json
import sys
from contextlib import asynccontextmanager

# Отключаем логгирование httpx
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

# Import discord_self
try:
    import discord_self as discord
    from discord_self.ext import commands
except ImportError:
    import discord
    from discord.ext import commands

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update, ChatMember
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, RetryAfter, BadRequest, Conflict
import httpx

# Load environment variables
load_dotenv()

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
DISCORD_USER_TOKEN = os.getenv('DISCORD_USER_TOKEN')
DISCORD_SERVER_ID = int(os.getenv('DISCORD_SERVER_ID', '0'))
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID', '0'))
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@your_channel')

# Admin IDs (comma separated)
ADMIN_IDS = [int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()]

# Proxy configuration
PROXY_HOST = os.getenv('PROXY_HOST', '127.0.0.1')
PROXY_PORT = int(os.getenv('PROXY_PORT', '1080'))
USE_PROXY = os.getenv('USE_PROXY', 'true').lower() == 'true'

# Database and translations
from database.db import *
from utils.translations import get_text, TRANSLATIONS

# Initialize database
init_db()

# Global variables
market_data: Dict[str, Dict] = {}
subscribers: Set[str] = set()
last_update_time: Optional[datetime] = None
last_message_id: Dict[str, int] = {}
discord_client: Optional[commands.Bot] = None
is_loading: bool = False
bot_instance: Optional[Bot] = None
price_history: List[Dict] = []
last_price_update: Optional[datetime] = None
start_time: Optional[datetime] = None
discord_ready = False
reconnect_attempts = 0
max_reconnect_attempts = 10
discord_connecting = False

# Target notifications
targets: Dict[str, Dict[str, int]] = {}  # chat_id: {item: target_price}

# Items list
ITEMS_LIST = [
    'Supernova Charge', 'Quantum Core', 'Antimatter', 
    'Alien Essence', 'Dark Matter', 'Robo Head', 
    'Data Cube', 'Stable Uran', 'Uran Ore', 
    'Diamonds', 'Research', 'Coin Bag', 
    'Gold', 'Cement', 'Oil'
]

# Кэш для языков пользователей (чтобы не ходить в БД каждый раз)
_language_cache: Dict[int, str] = {}
_cache_timestamp: Dict[int, datetime] = {}
CACHE_TTL = 300  # 5 минут

def escape_html(text: str) -> str:
    """Escape HTML special characters"""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is subscribed to the channel"""
    try:
        if not CHANNEL_ID or CHANNEL_ID == '@your_channel':
            return True
            
        chat_member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return chat_member.status in [ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER]
    except Exception as e:
        logger.error(f"Error checking subscription: {e}")
        return True

# ============= ОПТИМИЗИРОВАННАЯ РАБОТА С БД =============

def get_cached_language(user_id: int) -> str:
    """Получение языка пользователя с кэшированием"""
    global _language_cache, _cache_timestamp
    
    now = datetime.now()
    
    # Проверяем кэш
    if user_id in _language_cache and user_id in _cache_timestamp:
        if (now - _cache_timestamp[user_id]).seconds < CACHE_TTL:
            return _language_cache[user_id]
    
    # Если нет в кэше или устарел - получаем из БД
    lang = get_user_language(user_id)
    _language_cache[user_id] = lang
    _cache_timestamp[user_id] = now
    
    return lang

def invalidate_language_cache(user_id: int = None):
    """Инвалидация кэша языков"""
    global _language_cache, _cache_timestamp
    if user_id:
        _language_cache.pop(user_id, None)
        _cache_timestamp.pop(user_id, None)
    else:
        _language_cache.clear()
        _cache_timestamp.clear()

# ============= АВТОМАТИЧЕСКОЕ ОПРЕДЕЛЕНИЕ ЯЗЫКА =============

async def detect_user_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Автоматическое определение языка пользователя"""
    user_id = update.effective_user.id
    
    # Сначала проверяем сохраненный язык
    lang = get_cached_language(user_id)
    if lang != 'en':  # Если уже выбран не английский
        return lang
    
    # Определяем по языку интерфейса Telegram
    user = update.effective_user
    if user and user.language_code:
        # Если язык начинается с 'ru' - русский
        if user.language_code.startswith('ru'):
            lang = 'ru'
        # Можно добавить другие языки
        elif user.language_code.startswith('uk'):  # Украинский -> русский
            lang = 'ru'
        elif user.language_code.startswith('be'):  # Белорусский -> русский
            lang = 'ru'
        else:
            lang = 'en'
        
        # Сохраняем определенный язык
        set_user_language(user_id, lang)
        _language_cache[user_id] = lang
        _cache_timestamp[user_id] = datetime.now()
        
        logger.info(f"🌍 Auto-detected language for user {user_id}: {lang} (from {user.language_code})")
        return lang
    
    return 'en'

# ============= ДИСКОРД КЛИЕНТ =============

class DiscordUserClient(commands.Bot):
    """Discord client for user account"""
    
    def __init__(self):
        connection_kwargs = {
            'auto_reconnect': True,
            'heartbeat_timeout': 60.0,
            'guild_subscriptions': False,
        }
        
        http_kwargs = {
            'headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
        }
        
        try:
            super().__init__(
                command_prefix='!',
                self_bot=True,
                help_command=None,
                http_kwargs=http_kwargs,
                **connection_kwargs
            )
        except TypeError:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.guilds = True
            intents.guild_messages = True
            super().__init__(
                command_prefix='!',
                intents=intents,
                help_command=None,
                http_kwargs=http_kwargs,
                **connection_kwargs
            )
        
        self.target_server_id = DISCORD_SERVER_ID
        self.target_channel_id = DISCORD_CHANNEL_ID
        self.last_message_id = None
        self.is_ready_flag = False
        self.processed_messages: Set[str] = set()
        self.reconnect_delay = 5
    
    async def on_ready(self):
        global discord_ready, reconnect_attempts, discord_connecting
        discord_ready = True
        discord_connecting = False
        reconnect_attempts = 0
        logger.info(f'✅ Discord account connected as {self.user}')
        self.is_ready_flag = True
        
        server = self.get_guild(self.target_server_id)
        if server:
            logger.info(f'✅ Found server: {server.name}')
            channel = server.get_channel(self.target_channel_id)
            if channel:
                logger.info(f'✅ Found channel: {channel.name}')
                await self.fetch_latest_prices()
                asyncio.create_task(self.monitor_new_messages())
            else:
                logger.error(f'❌ Channel {self.target_channel_id} not found')
        else:
            logger.error(f'❌ Server {self.target_server_id} not found')
    
    async def on_disconnect(self):
        global discord_ready
        discord_ready = False
        self.is_ready_flag = False
        logger.warning('⚠️ Discord disconnected')
    
    async def on_error(self, event, *args, **kwargs):
        logger.error(f'❌ Discord error in {event}: {args}')
    
    async def monitor_new_messages(self):
        while True:
            try:
                if not self.is_ready_flag:
                    await asyncio.sleep(5)
                    continue
                
                channel = self.get_channel(self.target_channel_id)
                if not channel:
                    await asyncio.sleep(10)
                    continue
                
                async for msg in channel.history(limit=5):
                    if msg.id not in self.processed_messages:
                        has_prices = False
                        
                        if 'Supernova Charge' in msg.content or 'Market Prices' in msg.content:
                            has_prices = True
                        
                        if not has_prices and msg.embeds:
                            for embed in msg.embeds:
                                if embed.title and ('Market' in embed.title or 'Price' in embed.title):
                                    has_prices = True
                                    break
                                if embed.description and ('Supernova' in embed.description or 'Market' in embed.description):
                                    has_prices = True
                                    break
                        
                        if has_prices:
                            logger.info(f'📨 New price message detected: {msg.id}')
                            self.processed_messages.add(msg.id)
                            
                            if 'Supernova Charge' in msg.content or 'Market Prices' in msg.content:
                                await self.process_price_message(msg)
                            elif msg.embeds:
                                for embed in msg.embeds:
                                    if embed.title and ('Market' in embed.title or 'Price' in embed.title):
                                        await self.process_embed_message(embed)
                                        break
                                    if embed.description and ('Supernova' in embed.description or 'Market' in embed.description):
                                        await self.process_embed_message(embed)
                                        break
                            break
                
                if len(self.processed_messages) > 100:
                    self.processed_messages = set(list(self.processed_messages)[-100:])
                
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f'❌ Monitor error: {e}')
                await asyncio.sleep(10)
    
    async def fetch_latest_prices(self):
        try:
            if not self.is_ready_flag:
                logger.warning('Client not ready')
                return
            
            channel = self.get_channel(self.target_channel_id)
            if not channel:
                logger.error(f'❌ Channel not found')
                return
            
            logger.info('📡 Fetching latest messages from channel...')
            messages = []
            async for msg in channel.history(limit=50):
                messages.append(msg)
            
            logger.info(f'📨 Received {len(messages)} messages')
            
            for msg in messages:
                if 'Supernova Charge' in msg.content or 'Market Prices' in msg.content:
                    logger.info(f'✅ Found latest text message with prices (ID: {msg.id})')
                    self.processed_messages.add(msg.id)
                    await self.process_price_message(msg)
                    return
                
                if msg.embeds:
                    for embed in msg.embeds:
                        if embed.title and ('Market' in embed.title or 'Price' in embed.title):
                            logger.info(f'✅ Found latest embed: {embed.title}')
                            self.processed_messages.add(msg.id)
                            await self.process_embed_message(embed)
                            return
                        
                        if embed.description and ('Supernova' in embed.description or 'Market' in embed.description):
                            logger.info(f'✅ Found latest embed in description')
                            self.processed_messages.add(msg.id)
                            await self.process_embed_message(embed)
                            return
            
            logger.warning('⚠️ Price message not found')
                    
        except Exception as e:
            logger.error(f'❌ Error fetching prices: {e}')
    
    async def on_message(self, message):
        if not self.is_ready_flag:
            return
        
        if message.channel.id != self.target_channel_id:
            return
        
        if message.id in self.processed_messages:
            return
        
        if 'Supernova Charge' in message.content or 'Market Prices' in message.content:
            logger.info(f'📨 New text message with prices')
            self.processed_messages.add(message.id)
            await self.process_price_message(message)
            return
        
        if message.embeds:
            for embed in message.embeds:
                if embed.title and ('Market' in embed.title or 'Price' in embed.title):
                    logger.info(f'📨 New embed message with prices')
                    self.processed_messages.add(message.id)
                    await self.process_embed_message(embed)
                    return
                
                if embed.description and ('Supernova' in embed.description or 'Market' in embed.description):
                    logger.info(f'📨 New embed with prices in description')
                    self.processed_messages.add(message.id)
                    await self.process_embed_message(embed)
                    return
    
    async def process_embed_message(self, embed):
        try:
            content = ""
            if embed.title:
                content += embed.title + "\n"
            if embed.description:
                content += embed.description + "\n"
            if embed.fields:
                for field in embed.fields:
                    if field.name and field.value:
                        content += f"{field.name}\n{field.value}\n"
            if embed.author and embed.author.name:
                content += embed.author.name + "\n"
            
            await self.parse_prices_from_text(content)
            
        except Exception as e:
            logger.error(f'❌ Error parsing embed: {e}')
    
    async def process_price_message(self, message):
        try:
            await self.parse_prices_from_text(message.content)
        except Exception as e:
            logger.error(f'❌ Error parsing text: {e}')
    
    async def parse_prices_from_text(self, text: str):
        global market_data, last_update_time, is_loading, price_history, last_price_update
        
        try:
            text = re.sub(r'<:[a-zA-Z_]+:\d+>', '', text)
            text = re.sub(r'```diff\s*', '', text)
            text = re.sub(r'```\s*', '', text)
            text = re.sub(r'\n\s*\n', '\n', text)
            
            lines = text.split('\n')
            new_data = {}
            previous_prices = {k: v['raw_price'] for k, v in market_data.items()} if market_data else {}
            
            # Подготовка данных для массовой вставки в БД
            price_history_batch = []
            daily_best_batch = []
            
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if not line:
                    i += 1
                    continue
                
                for item in ITEMS_LIST:
                    if item.lower() in line.lower():
                        price_match = re.search(r'\$?([\d,]+)\$?', line)
                        if price_match:
                            price = int(price_match.group(1).replace(',', ''))
                            
                            change = '0%'
                            if i + 1 < len(lines):
                                next_line = lines[i + 1].strip()
                                change_match = re.search(r'([+-])\s*(\d+)%', next_line)
                                if change_match:
                                    sign = change_match.group(1)
                                    value = change_match.group(2)
                                    change = f'{sign}{value}%'
                            
                            new_data[item] = {
                                'price': f'${price:,}',
                                'change': change,
                                'raw_price': price
                            }
                            
                            # Добавляем в батч для истории цен
                            price_history_batch.append((item, price, change))
                            
                            # Расчет процента изменения для лучших продаж
                            if item in previous_prices and previous_prices[item] > 0:
                                change_percent = ((price - previous_prices[item]) / previous_prices[item]) * 100
                                # ТОЛЬКО если изменение >= 20%
                                if change_percent >= 20:
                                    daily_best_batch.append((item, price, change_percent))
                                    logger.info(f'🔥 BIG CHANGE: {item} +{change_percent:.1f}% (${previous_prices[item]:,} -> ${price:,})')
                            
                            logger.info(f'✅ Found item: {item} - ${price:,} ({change})')
                            break
                
                i += 1
            
            if new_data:
                # Массовая вставка в БД
                if price_history_batch:
                    save_price_history_batch(price_history_batch)
                
                if daily_best_batch:
                    save_daily_best_batch(daily_best_batch)
                
                old_data = {k: v['raw_price'] for k, v in market_data.items()} if market_data else {}
                
                if market_data and targets:
                    await check_targets(new_data)
                
                if market_data:
                    new_data_raw = {k: v['raw_price'] for k, v in new_data.items()}
                    
                    price_changes = {}
                    for item, price in new_data_raw.items():
                        if item in old_data:
                            change_percent = ((price - old_data[item]) / old_data[item]) * 100
                            if abs(change_percent) > 1:
                                price_changes[item] = change_percent
                    
                    price_history.append({
                        'timestamp': datetime.now(),
                        'data': new_data_raw,
                        'changes': price_changes
                    })
                    
                    if len(price_history) > 100:
                        price_history = price_history[-100:]
                
                market_data = new_data
                last_update_time = datetime.now()
                last_price_update = datetime.now()
                is_loading = False
                
                logger.info(f'✅ Prices updated: {len(market_data)} items')
                if daily_best_batch:
                    logger.info(f'🔥 {len(daily_best_batch)} items with +20%+ growth!')
                
                # Update subscribers from database
                db_subscribers = get_all_subscribers()
                for user_id in db_subscribers:
                    if str(user_id) not in subscribers:
                        subscribers.add(str(user_id))
                
                await update_all_subscribers()
            else:
                logger.warning('⚠️ Failed to parse prices')
                
        except Exception as e:
            logger.error(f'❌ Error parsing: {e}')

async def check_targets(new_data: Dict):
    global targets
    
    if not targets or not bot_instance:
        return
    
    # Собираем все уведомления для отправки
    notifications = []
    
    for chat_id, user_targets in list(targets.items()):
        for item, target_price in list(user_targets.items()):
            if item in new_data:
                current_price = new_data[item]['raw_price']
                if current_price >= target_price:
                    user_id = int(chat_id)
                    lang = get_cached_language(user_id)
                    notifications.append((chat_id, item, current_price, target_price, lang))
                    del targets[chat_id][item]
                    if not targets[chat_id]:
                        del targets[chat_id]
    
    # Сохраняем цели в БД
    if notifications:
        save_targets()
        
        # Отправляем уведомления параллельно
        tasks = []
        for chat_id, item, current_price, target_price, lang in notifications:
            tasks.append(
                bot_instance.send_message(
                    chat_id=chat_id,
                    text=get_text('target_reached', lang, 
                                item=escape_html(item), 
                                current=current_price, 
                                target=target_price),
                    parse_mode='HTML'
                )
            )
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

def save_targets():
    """Save targets to file"""
    try:
        with open('targets.json', 'w') as f:
            json.dump(targets, f)
    except Exception as e:
        logger.error(f'Error saving targets: {e}')

def load_targets():
    global targets
    try:
        if os.path.exists('targets.json'):
            with open('targets.json', 'r') as f:
                loaded_targets = json.load(f)
            
            targets = {}
            for chat_id, user_targets in loaded_targets.items():
                valid_targets = {}
                for item, price in user_targets.items():
                    if item in ITEMS_LIST:
                        valid_targets[item] = price
                    else:
                        logger.warning(f'⚠️ Unknown item "{item}" in targets, skipping')
                if valid_targets:
                    targets[chat_id] = valid_targets
            
            logger.info(f'✅ Loaded {sum(len(t) for t in targets.values())} targets')
    except Exception as e:
        logger.error(f'Error loading targets: {e}')

async def update_all_subscribers():
    if not subscribers or not market_data:
        return
    
    global bot_instance
    if not bot_instance:
        return
    
    # Подготовка данных для массовой рассылки
    tasks = []
    chat_ids = list(subscribers)
    
    for chat_id in chat_ids:
        try:
            user_id = int(chat_id)
            lang = get_cached_language(user_id)
            message = format_price_message(lang)
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
                [InlineKeyboardButton("❌ Unsubscribe" if lang == 'en' else "❌ Отписаться", callback_data="unsubscribe")]
            ])
            
            if chat_id in last_message_id:
                tasks.append(
                    edit_message_safe(chat_id, last_message_id[chat_id], message, keyboard, lang)
                )
            else:
                tasks.append(
                    send_message_safe(chat_id, message, keyboard, lang)
                )
                
        except Exception as e:
            logger.error(f'❌ Error preparing update for {chat_id}: {e}')
    
    # Параллельная отправка
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Обработка результатов
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                chat_id = chat_ids[i] if i < len(chat_ids) else 'unknown'
                logger.error(f'Error updating {chat_id}: {result}')
                if 'Forbidden' in str(result) or 'Chat not found' in str(result) or 'bot was blocked' in str(result):
                    subscribers.discard(chat_id)
                    try:
                        remove_subscriber(int(chat_id))
                    except:
                        pass
                    if chat_id in last_message_id:
                        del last_message_id[chat_id]

async def edit_message_safe(chat_id: str, message_id: int, message: str, keyboard, lang: str):
    """Безопасное редактирование сообщения"""
    try:
        await bot_instance.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=message,
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        logger.info(f'📝 Updated message for {chat_id}')
        return True
    except BadRequest as e:
        if 'message to edit not found' in str(e):
            msg = await bot_instance.send_message(
                chat_id=chat_id,
                text=message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            last_message_id[chat_id] = msg.message_id
            logger.info(f'📤 Sent new message to {chat_id} (old was deleted)')
            return True
        raise
    except Exception as e:
        logger.warning(f'Could not edit message for {chat_id}: {e}')
        msg = await bot_instance.send_message(
            chat_id=chat_id,
            text=message,
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        last_message_id[chat_id] = msg.message_id
        return True

async def send_message_safe(chat_id: str, message: str, keyboard, lang: str):
    """Безопасная отправка сообщения"""
    try:
        msg = await bot_instance.send_message(
            chat_id=chat_id,
            text=message,
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        last_message_id[chat_id] = msg.message_id
        logger.info(f'📤 New message sent to {chat_id}')
        return True
    except Exception as e:
        raise e

def format_price_message(lang: str = 'en') -> str:
    if not market_data:
        return get_text('no_price_data', lang)
    
    sorted_items = sorted(
        market_data.items(),
        key=lambda x: x[1]['raw_price'],
        reverse=True
    )
    
    now = datetime.now()
    minutes = now.minute
    next_minute = ((minutes // 3) + 1) * 3
    if next_minute >= 60:
        next_minute = 0
    next_time = now.replace(minute=next_minute, second=0, microsecond=0)
    if next_time <= now:
        next_time += timedelta(hours=1)
    
    title = "📊 MARKET PRICES" if lang == 'en' else "📊 Цены на рынке"
    updated = f"🕐 Updated: {last_update_time.strftime('%H:%M:%S') if last_update_time else 'Never'}" if lang == 'en' else f"🕐 Обновлено: {last_update_time.strftime('%H:%M:%S') if last_update_time else 'Никогда'}"
    next_update = f"⏱ Next update: {next_time.strftime('%H:%M:%S')}" if lang == 'en' else f"⏱ Следующее обновление: {next_time.strftime('%H:%M:%S')}"
    
    lines = [
        title,
        updated,
        next_update,
        "─" * 35,
        ""
    ]
    
    for name, data in sorted_items:
        change = data['change']
        if '+' in change:
            change_emoji = "📈"
        elif '-' in change:
            change_emoji = "📉"
        else:
            change_emoji = "➖"
        
        lines.append(
            f"<b>{escape_html(name)}</b>\n"
            f"  💰 {data['price']}  {change_emoji} {change}"
        )
    
    lines.append("")
    lines.append("─" * 35)
    lines.append(f"🔄 Auto-updates every 3 minutes" if lang == 'en' else "🔄 Авто-обновление каждые 3 минуты")
    lines.append(f"👥 Subscribers: {len(subscribers)}" if lang == 'en' else f"👥 Подписчиков: {len(subscribers)}")
    
    return "\n".join(lines)

# ============= LANGUAGE SELECTION =============

async def language_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    lang = query.data.split('_')[1]
    
    set_user_language(user_id, lang)
    invalidate_language_cache(user_id)
    
    text = get_text('language_selected', lang) if lang == 'en' else get_text('language_selected_ru', 'ru')
    
    await query.edit_message_text(text)
    await show_main_menu_from_callback(query, context)

async def show_main_menu_from_callback(query, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    lang = get_cached_language(user_id)
    user = query.from_user
    
    if is_banned(user_id):
        await query.message.reply_text("🚫 You are banned from using this bot.")
        return
    
    if not await check_subscription(user_id, context):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Subscribe" if lang == 'en' else "📢 Подписаться", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")],
            [InlineKeyboardButton("✅ Check Subscription" if lang == 'en' else "✅ Проверить подписку", callback_data="check_sub")]
        ])
        await query.message.reply_text(
            get_text('must_subscribe', lang, channel_link=CHANNEL_ID),
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        return
    
    create_user(user_id, user.username, user.first_name)
    
    keyboard_buttons = [
        [InlineKeyboardButton("📊 " + ("Get Prices" if lang == 'en' else "Цены"), callback_data="price")],
        [InlineKeyboardButton("🔔 " + ("Subscribe" if lang == 'en' else "Подписаться"), callback_data="subscribe")],
        [InlineKeyboardButton("❌ " + ("Unsubscribe" if lang == 'en' else "Отписаться"), callback_data="unsubscribe")],
        [InlineKeyboardButton("🎯 " + ("Targets" if lang == 'en' else "Цели"), callback_data="targets")],
        [InlineKeyboardButton("📈 " + ("Best Sales" if lang == 'en' else "Лучшие продажи"), callback_data="best_sales")]
    ]
    
    if user_id in ADMIN_IDS:
        keyboard_buttons.append([InlineKeyboardButton("🔐 Admin Panel", callback_data="admin_panel")])
    
    keyboard = InlineKeyboardMarkup(keyboard_buttons)
    
    welcome_text = get_text('start', lang, name=user.first_name)
    
    await query.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode='HTML')

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_cached_language(user_id)
    user = update.effective_user
    
    if is_banned(user_id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return
    
    if not await check_subscription(user_id, context):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Subscribe" if lang == 'en' else "📢 Подписаться", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")],
            [InlineKeyboardButton("✅ Check Subscription" if lang == 'en' else "✅ Проверить подписку", callback_data="check_sub")]
        ])
        await update.message.reply_text(
            get_text('must_subscribe', lang, channel_link=CHANNEL_ID),
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        return
    
    create_user(user_id, user.username, user.first_name)
    
    keyboard_buttons = [
        [InlineKeyboardButton("📊 " + ("Get Prices" if lang == 'en' else "Цены"), callback_data="price")],
        [InlineKeyboardButton("🔔 " + ("Subscribe" if lang == 'en' else "Подписаться"), callback_data="subscribe")],
        [InlineKeyboardButton("❌ " + ("Unsubscribe" if lang == 'en' else "Отписаться"), callback_data="unsubscribe")],
        [InlineKeyboardButton("🎯 " + ("Targets" if lang == 'en' else "Цели"), callback_data="targets")],
        [InlineKeyboardButton("📈 " + ("Best Sales" if lang == 'en' else "Лучшие продажи"), callback_data="best_sales")]
    ]
    
    if user_id in ADMIN_IDS:
        keyboard_buttons.append([InlineKeyboardButton("🔐 Admin Panel", callback_data="admin_panel")])
    
    keyboard = InlineKeyboardMarkup(keyboard_buttons)
    
    welcome_text = get_text('start', lang, name=user.first_name)
    
    if update.callback_query:
        await update.callback_query.message.edit_text(welcome_text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode='HTML')

# ============= COMMAND HANDLERS =============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if is_banned(user_id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return
    
    # Автоматическое определение языка
    lang = await detect_user_language(update, context)
    
    # Если язык уже определен и не английский - сразу показываем меню
    if lang != 'en':
        await show_main_menu(update, context)
        return
    
    # Иначе показываем выбор языка
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")]
    ])
    
    await update.message.reply_text(
        "🌍 Please choose your language / Пожалуйста, выберите язык:",
        reply_markup=keyboard
    )

async def check_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    lang = get_cached_language(user_id)
    
    await query.edit_message_text(get_text('check_subscription', lang))
    
    if await check_subscription(user_id, context):
        create_user(user_id, update.effective_user.username, update.effective_user.first_name)
        await query.edit_message_text(get_text('subscribed', lang))
        await show_main_menu(update, context)
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Subscribe" if lang == 'en' else "📢 Подписаться", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")],
            [InlineKeyboardButton("✅ Check Subscription" if lang == 'en' else "✅ Проверить подписку", callback_data="check_sub")]
        ])
        await query.edit_message_text(
            get_text('not_subscribed', lang),
            reply_markup=keyboard,
            parse_mode='HTML'
        )

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if is_banned(user_id):
        await update.message.reply_text("🚫 You are banned.")
        return
    
    lang = get_cached_language(user_id)
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    if not market_data:
        await update.message.reply_text(get_text('no_price_data', lang))
        if discord_client and discord_client.is_ready_flag:
            await discord_client.fetch_latest_prices()
        return
    
    message = format_price_message(lang)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
        [InlineKeyboardButton("🔔 Subscribe" if lang == 'en' else "🔔 Подписаться", callback_data="subscribe")]
    ])
    
    await update.message.reply_text(message, reply_markup=keyboard, parse_mode='HTML')

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    lang = get_cached_language(user_id)
    
    if is_banned(user_id):
        await update.message.reply_text("🚫 You are banned.")
        return
    
    subscribers.add(chat_id)
    add_subscriber(user_id)
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Get Prices" if lang == 'en' else "📊 Цены", callback_data="price")],
        [InlineKeyboardButton("❌ Unsubscribe" if lang == 'en' else "❌ Отписаться", callback_data="unsubscribe")]
    ])
    
    try:
        msg = await update.message.reply_text(
            get_text('subscribe', lang),
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        last_message_id[chat_id] = msg.message_id
    except Exception as e:
        logger.error(f'Error in subscribe: {e}')

async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    lang = get_cached_language(user_id)
    
    subscribers.discard(chat_id)
    remove_subscriber(user_id)
    if chat_id in last_message_id:
        del last_message_id[chat_id]
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Get Prices" if lang == 'en' else "📊 Цены", callback_data="price")],
        [InlineKeyboardButton("🔔 Subscribe" if lang == 'en' else "🔔 Подписаться", callback_data="subscribe")]
    ])
    
    try:
        msg = await update.message.reply_text(
            get_text('unsubscribe', lang),
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        last_message_id[chat_id] = msg.message_id
    except Exception as e:
        logger.error(f'Error in unsubscribe: {e}')

async def targets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    lang = get_cached_language(user_id)
    
    if is_banned(user_id):
        await update.message.reply_text("🚫 You are banned.")
        return
    
    user_targets = get_user_targets(user_id)
    
    if not user_targets:
        await update.message.reply_text(
            "🎯 <b>Your Targets</b>\n\n"
            "You have no active targets.\n\n"
            "Use /settarget to create a target.\n"
            "Example: /settarget Supernova Charge 50000" if lang == 'en' else
            "🎯 <b>Ваши цели</b>\n\n"
            "У вас нет активных целей.\n\n"
            "Используйте /settarget для создания цели.\n"
            "Пример: /settarget Supernova Charge 50000",
            parse_mode='HTML'
        )
        return
    
    target_list = []
    for item, price in user_targets.items():
        current = market_data.get(item, {}).get('raw_price', 0)
        status = "✅" if current >= price else "⏳"
        target_list.append(f"{status} {escape_html(item)}: ${price:,} (Current: ${current:,})")
    
    text = (
        "🎯 <b>Your Targets</b>\n\n" if lang == 'en' else "🎯 <b>Ваши цели</b>\n\n"
        + "\n".join(target_list) +
        ("\n\nUse /removetarget <item> to remove a target" if lang == 'en' else "\n\nИспользуйте /removetarget <item> для удаления цели")
    )
    
    await update.message.reply_text(text, parse_mode='HTML')

async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    lang = get_cached_language(user_id)
    
    if is_banned(user_id):
        await update.message.reply_text("🚫 You are banned.")
        return
    
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Usage: /settarget <item> <price>\n\n"
            "Example: /settarget Supernova Charge 50000\n\n"
            f"Available items:\n• " + "\n• ".join(ITEMS_LIST) if lang == 'en' else
            "❌ Использование: /settarget <предмет> <цена>\n\n"
            "Пример: /settarget Supernova Charge 50000\n\n"
            f"Доступные предметы:\n• " + "\n• ".join(ITEMS_LIST),
            parse_mode='HTML'
        )
        return
    
    price_str = args[-1]
    item_name = ' '.join(args[:-1])
    
    try:
        target_price = int(price_str.replace(',', ''))
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Use numbers only." if lang == 'en' else "❌ Неверная цена. Используйте только числа.")
        return
    
    matched_item = None
    for item in ITEMS_LIST:
        if item.lower() == item_name.lower():
            matched_item = item
            break
    
    if not matched_item:
        await update.message.reply_text(
            f"❌ Item '{escape_html(item_name)}' not found.\n\n"
            f"Available items:\n• " + "\n• ".join(ITEMS_LIST) if lang == 'en' else
            f"❌ Предмет '{escape_html(item_name)}' не найден.\n\n"
            f"Доступные предметы:\n• " + "\n• ".join(ITEMS_LIST)
        )
        return
    
    save_target(user_id, matched_item, target_price)
    
    if chat_id not in targets:
        targets[chat_id] = {}
    targets[chat_id][matched_item] = target_price
    save_targets()
    
    await update.message.reply_text(
        f"✅ Target set!\n\n"
        f"📦 {escape_html(matched_item)}\n"
        f"🎯 Target: ${target_price:,}\n\n"
        f"I'll notify you when the price reaches ${target_price:,}" if lang == 'en' else
        f"✅ Цель установлена!\n\n"
        f"📦 {escape_html(matched_item)}\n"
        f"🎯 Цель: ${target_price:,}\n\n"
        f"Я уведомлю вас, когда цена достигнет ${target_price:,}",
        parse_mode='HTML'
    )

async def remove_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = str(update.effective_chat.id)
    lang = get_cached_language(user_id)
    
    if is_banned(user_id):
        await update.message.reply_text("🚫 You are banned.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /removetarget <item>\n"
            "Example: /removetarget Supernova Charge" if lang == 'en' else
            "❌ Использование: /removetarget <предмет>\n"
            "Пример: /removetarget Supernova Charge"
        )
        return
    
    item_name = ' '.join(context.args)
    
    matched_item = None
    for item in ITEMS_LIST:
        if item.lower() == item_name.lower():
            matched_item = item
            break
    
    if not matched_item:
        await update.message.reply_text(f"❌ Item '{escape_html(item_name)}' not found." if lang == 'en' else f"❌ Предмет '{escape_html(item_name)}' не найден.")
        return
    
    remove_target(user_id, matched_item)
    
    if chat_id in targets and matched_item in targets[chat_id]:
        del targets[chat_id][matched_item]
        if not targets[chat_id]:
            del targets[chat_id]
        save_targets()
    
    await update.message.reply_text(f"✅ Target for {escape_html(matched_item)} removed!" if lang == 'en' else f"✅ Цель для {escape_html(matched_item)} удалена!")

async def best_sales_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_cached_language(user_id)
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only command." if lang == 'en' else "❌ Только для администраторов.")
        return
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    # Получаем лучшие продажи ЗА ВСЕ ВРЕМЯ (или за сегодня)
    sales = get_daily_best_all()
    
    if not sales:
        await update.message.reply_text(
            "No sales data with +20% growth found." if lang == 'en' else "Нет данных о продажах с ростом +20%."
        )
        return
    
    message = "📈 <b>BEST SALES (+20%+ GROWTH)</b>\n\n" if lang == 'en' else "📈 <b>ЛУЧШИЕ ПРОДАЖИ (+20%+ РОСТ)</b>\n\n"
    
    for sale in sales[:20]:
        message += f"📦 {escape_html(sale['item'])}: ${sale['price']:,} (+{sale['change_percent']:.1f}%)\n"
        message += f"   📅 {sale['date']}\n"
    
    if len(sales) > 20:
        message += f"\n... and {len(sales) - 20} more" if lang == 'en' else f"\n... и еще {len(sales) - 20}"
    
    await update.message.reply_text(message, parse_mode='HTML')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    lang = get_cached_language(user_id)
    
    help_text = get_text('help', lang)
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Get Prices" if lang == 'en' else "📊 Цены", callback_data="price")],
        [InlineKeyboardButton("🔔 Subscribe" if lang == 'en' else "🔔 Подписаться", callback_data="subscribe")]
    ])
    
    try:
        msg = await update.message.reply_text(
            help_text,
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        last_message_id[chat_id] = msg.message_id
    except Exception as e:
        logger.error(f'Error in help: {e}')

# ============= ADMIN COMMANDS =============

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_cached_language(user_id)
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text(get_text('not_admin', lang))
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Bot Status" if lang == 'en' else "📊 Статус бота", callback_data="admin_status")],
        [InlineKeyboardButton("👥 Subscribers List" if lang == 'en' else "👥 Список подписчиков", callback_data="admin_subscribers")],
        [InlineKeyboardButton("📢 Broadcast" if lang == 'en' else "📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔄 Force Update" if lang == 'en' else "🔄 Принудительное обновление", callback_data="admin_force_update")],
        [InlineKeyboardButton("📈 Price Stats" if lang == 'en' else "📈 Статистика цен", callback_data="admin_stats")],
        [InlineKeyboardButton("🎯 All Targets" if lang == 'en' else "🎯 Все цели", callback_data="admin_targets")],
        [InlineKeyboardButton("🚫 Blacklist" if lang == 'en' else "🚫 Черный список", callback_data="admin_blacklist")],
        [InlineKeyboardButton("🔄 Reconnect Discord" if lang == 'en' else "🔄 Переподключить Discord", callback_data="admin_reconnect")],
        [InlineKeyboardButton("❌ Close" if lang == 'en' else "❌ Закрыть", callback_data="admin_close")]
    ])
    
    await update.message.reply_text(
        get_text('admin_panel', lang),
        reply_markup=keyboard,
        parse_mode='HTML'
    )

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    lang = get_cached_language(user_id)
    
    if user_id not in ADMIN_IDS:
        await query.answer(get_text('not_admin', lang), show_alert=True)
        return
    
    await query.answer()
    
    if query.data == "admin_status":
        status_text = (
            "📊 <b>Bot Status</b>\n\n"
            f"✅ Discord: {'✅ Connected' if discord_client and discord_client.is_ready_flag else '❌ Disconnected'}\n"
            f"📡 Market Data: {len(market_data)} items\n"
            f"👥 Subscribers: {len(subscribers)}\n"
            f"🎯 Active Targets: {sum(len(t) for t in targets.values())}\n"
            f"🕐 Last Update: {last_update_time.strftime('%H:%M:%S') if last_update_time else 'Never'}\n"
            f"💾 History Entries: {len(price_history)}\n"
            f"🤖 Uptime: {(datetime.now() - start_time).seconds // 60} minutes\n"
            f"📅 Started: {start_time.strftime('%H:%M:%S') if start_time else 'Never'}\n"
            f"🔄 Reconnect Attempts: {reconnect_attempts}"
        )
        await query.edit_message_text(status_text, parse_mode='HTML')
        
    elif query.data == "admin_subscribers":
        subscribers_list = get_all_subscribers()
        if not subscribers_list:
            await query.edit_message_text("No subscribers yet." if lang == 'en' else "Нет подписчиков.")
            return
        
        sub_list = "\n".join([f"• {user_id}" for user_id in list(subscribers_list)[:20]])
        if len(subscribers_list) > 20:
            sub_list += f"\n... and {len(subscribers_list) - 20} more" if lang == 'en' else f"\n... и еще {len(subscribers_list) - 20}"
        
        await query.edit_message_text(
            f"👥 <b>Subscribers ({len(subscribers_list)})</b>\n\n{sub_list}",
            parse_mode='HTML'
        )
        
    elif query.data == "admin_broadcast":
        context.user_data['broadcast_mode'] = True
        await query.edit_message_text(
            "📢 <b>Broadcast Mode</b>\n\n"
            "Send me the message you want to broadcast to all subscribers.\n"
            "Use /cancel to cancel." if lang == 'en' else
            "📢 <b>Режим рассылки</b>\n\n"
            "Отправьте мне сообщение для рассылки всем подписчикам.\n"
            "Используйте /cancel для отмены.",
            parse_mode='HTML'
        )
        
    elif query.data == "admin_force_update":
        await query.edit_message_text("🔄 Forcing price update..." if lang == 'en' else "🔄 Принудительное обновление цен...")
        if discord_client and discord_client.is_ready_flag:
            await discord_client.fetch_latest_prices()
            await query.edit_message_text("✅ Price update forced!" if lang == 'en' else "✅ Цены обновлены принудительно!")
        else:
            await query.edit_message_text("❌ Discord client not ready!" if lang == 'en' else "❌ Discord клиент не готов!")
            
    elif query.data == "admin_stats":
        if not price_history:
            await query.edit_message_text("No price history yet." if lang == 'en' else "Нет истории цен.")
            return
        
        latest = price_history[-1] if price_history else {}
        changes = latest.get('changes', {})
        
        if changes:
            stats_text = "📈 <b>Recent Price Changes</b>\n\n"
            sorted_changes = sorted(changes.items(), key=lambda x: abs(x[1]), reverse=True)
            for item, change in sorted_changes[:10]:
                emoji = "📈" if change > 0 else "📉"
                stats_text += f"{emoji} {escape_html(item)}: {change:+.1f}%\n"
        else:
            stats_text = "No significant changes detected." if lang == 'en' else "Нет значительных изменений."
        
        await query.edit_message_text(stats_text, parse_mode='HTML')
    
    elif query.data == "admin_targets":
        all_targets = get_all_targets()
        if not all_targets:
            await query.edit_message_text("No active targets." if lang == 'en' else "Нет активных целей.")
            return
        
        target_text = "🎯 <b>All Targets</b>\n\n"
        for user_id, user_targets in all_targets.items():
            target_text += f"User {user_id}:\n"
            for item, price in user_targets.items():
                target_text += f"  • {escape_html(item)}: ${price:,}\n"
            target_text += "\n"
        
        await query.edit_message_text(target_text, parse_mode='HTML')
    
    elif query.data == "admin_blacklist":
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, reason, banned_at FROM blacklist ORDER BY banned_at DESC')
        results = cursor.fetchall()
        conn.close()
        
        if not results:
            await query.edit_message_text("Blacklist is empty." if lang == 'en' else "Черный список пуст.")
            return
        
        message = "🚫 Blacklist:\n\n"
        for row in results[:10]:
            message += f"• {row['user_id']}: {row['reason'] or 'No reason'}\n"
        
        if len(results) > 10:
            message += f"\n... and {len(results) - 10} more"
        
        message += "\n\nUse /blacklist add <user_id> [reason] to ban\nUse /blacklist remove <user_id> to unban"
        
        await query.edit_message_text(message)
    
    elif query.data == "admin_reconnect":
        await query.edit_message_text("🔄 Attempting to reconnect Discord..." if lang == 'en' else "🔄 Попытка переподключения Discord...")
        if discord_client:
            try:
                await discord_client.close()
                await asyncio.sleep(2)
                await discord_client.start(DISCORD_USER_TOKEN)
                await query.edit_message_text("✅ Reconnect initiated!" if lang == 'en' else "✅ Переподключение инициировано!")
            except Exception as e:
                await query.edit_message_text(f"❌ Reconnect failed: {e}" if lang == 'en' else f"❌ Ошибка переподключения: {e}")
        else:
            await query.edit_message_text("❌ Discord client not available" if lang == 'en' else "❌ Discord клиент недоступен")
        
    elif query.data == "admin_close":
        await query.edit_message_text("🔐 Admin panel closed." if lang == 'en' else "🔐 Панель администратора закрыта.")

async def admin_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_cached_language(user_id)
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return
    
    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "Usage:\n"
            "/blacklist add <user_id> [reason] - Ban user\n"
            "/blacklist remove <user_id> - Unban user\n"
            "/blacklist list - Show blacklist"
        )
        return
    
    action = args[0].lower()
    
    if action == 'add' and len(args) >= 2:
        try:
            target_id = int(args[1])
            reason = ' '.join(args[2:]) if len(args) > 2 else 'No reason'
            add_to_blacklist(target_id, reason)
            
            await update.message.reply_text(
                f"✅ User {target_id} banned.\nReason: {reason}" if lang == 'en' else
                f"✅ Пользователь {target_id} забанен.\nПричина: {reason}"
            )
            
            try:
                user_lang = get_cached_language(target_id)
                await context.bot.send_message(
                    chat_id=target_id,
                    text=f"🚫 You have been banned from the bot.\nReason: {reason}" if user_lang == 'en' else
                         f"🚫 Вы были забанены в боте.\nПричина: {reason}"
                )
            except:
                pass
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Use numbers only.")
    
    elif action == 'remove' and len(args) >= 2:
        try:
            target_id = int(args[1])
            remove_from_blacklist(target_id)
            await update.message.reply_text(
                f"✅ User {target_id} unbanned." if lang == 'en' else
                f"✅ Пользователь {target_id} разбанен."
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Use numbers only.")
    
    elif action == 'list':
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, reason, banned_at FROM blacklist ORDER BY banned_at DESC')
        results = cursor.fetchall()
        conn.close()
        
        if not results:
            await update.message.reply_text("Blacklist is empty." if lang == 'en' else "Черный список пуст.")
            return
        
        message = "🚫 Blacklist:\n\n"
        for row in results[:10]:
            message += f"• {row['user_id']}: {row['reason'] or 'No reason'}\n"
        
        if len(results) > 10:
            message += f"\n... and {len(results) - 10} more"
        
        await update.message.reply_text(message)
    
    else:
        await update.message.reply_text(
            "Usage:\n"
            "/blacklist add <user_id> [reason] - Ban user\n"
            "/blacklist remove <user_id> - Unban user\n"
            "/blacklist list - Show blacklist"
        )

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('broadcast_mode'):
        return
    
    user_id = update.effective_user.id
    lang = get_cached_language(user_id)
    
    if user_id not in ADMIN_IDS:
        return
    
    message_text = update.message.text
    if message_text == '/cancel':
        context.user_data['broadcast_mode'] = False
        await update.message.reply_text("❌ Broadcast cancelled." if lang == 'en' else "❌ Рассылка отменена.")
        return
    
    subscribers_list = get_all_subscribers()
    if not subscribers_list:
        await update.message.reply_text("❌ No subscribers to broadcast to." if lang == 'en' else "❌ Нет подписчиков для рассылки.")
        context.user_data['broadcast_mode'] = False
        return
    
    context.user_data['broadcast_mode'] = False
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    progress_msg = await update.message.reply_text(
        f"📤 Broadcasting to {len(subscribers_list)} subscribers..." if lang == 'en' else
        f"📤 Рассылка {len(subscribers_list)} подписчикам..."
    )
    
    # Массовая рассылка с использованием asyncio.gather
    tasks = []
    for user_id in subscribers_list:
        user_lang = get_cached_language(user_id)
        tasks.append(
            bot_instance.send_message(
                chat_id=user_id,
                text=f"📢 <b>Announcement</b>\n\n{escape_html(message_text)}",
                parse_mode='HTML'
            )
        )
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    success = sum(1 for r in results if not isinstance(r, Exception))
    failed = len(results) - success
    
    await progress_msg.edit_text(
        f"✅ Broadcast complete!\n\n"
        f"✅ Success: {success}\n"
        f"❌ Failed: {failed}" if lang == 'en' else
        f"✅ Рассылка завершена!\n\n"
        f"✅ Успешно: {success}\n"
        f"❌ Ошибок: {failed}"
    )

# ============= BUTTON CALLBACKS =============

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    try:
        for attempt in range(3):
            try:
                await query.answer()
                break
            except (TimedOut, NetworkError) as e:
                if attempt == 2:
                    logger.error(f'Failed to answer callback after 3 attempts: {e}')
                    return
                await asyncio.sleep(1)
    except Exception as e:
        logger.error(f'Error in callback answer: {e}')
        return
    
    chat_id = str(query.message.chat.id)
    user_id = update.effective_user.id
    lang = get_cached_language(user_id)
    
    if is_banned(user_id):
        await query.edit_message_text("🚫 You are banned from using this bot.")
        return
    
    if query.data == "admin_panel":
        if user_id in ADMIN_IDS:
            await query.message.delete()
            await admin_menu(update, context)
        else:
            await query.edit_message_text(get_text('not_admin', lang))
        return
    
    if query.data.startswith("admin_"):
        await admin_callback(update, context)
        return
    
    if query.data.startswith("lang_"):
        await language_selection(update, context)
        return
    
    if query.data == "check_sub":
        await check_subscription_callback(update, context)
        return
    
    try:
        if query.data == "price" or query.data == "refresh":
            await context.bot.send_chat_action(chat_id=chat_id, action='typing')
            
            if not market_data:
                await query.edit_message_text(get_text('no_price_data', lang))
                if discord_client and discord_client.is_ready_flag:
                    await discord_client.fetch_latest_prices()
                return
            
            message = format_price_message(lang)
            keyboard_buttons = [
                [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
                [InlineKeyboardButton("🔔 Subscribe" if lang == 'en' else "🔔 Подписаться", callback_data="subscribe")]
            ]
            
            if user_id in ADMIN_IDS:
                keyboard_buttons.append([InlineKeyboardButton("🔐 Admin Panel", callback_data="admin_panel")])
            
            keyboard = InlineKeyboardMarkup(keyboard_buttons)
            
            await query.edit_message_text(
                message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            last_message_id[chat_id] = query.message.message_id
        
        elif query.data == "subscribe":
            subscribers.add(chat_id)
            add_subscriber(user_id)
            
            keyboard_buttons = [
                [InlineKeyboardButton("📊 Get Prices" if lang == 'en' else "📊 Цены", callback_data="price")],
                [InlineKeyboardButton("❌ Unsubscribe" if lang == 'en' else "❌ Отписаться", callback_data="unsubscribe")]
            ]
            
            if user_id in ADMIN_IDS:
                keyboard_buttons.append([InlineKeyboardButton("🔐 Admin Panel", callback_data="admin_panel")])
            
            keyboard = InlineKeyboardMarkup(keyboard_buttons)
            
            await query.edit_message_text(
                get_text('subscribe', lang),
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            last_message_id[chat_id] = query.message.message_id
        
        elif query.data == "unsubscribe":
            subscribers.discard(chat_id)
            remove_subscriber(user_id)
            if chat_id in last_message_id:
                del last_message_id[chat_id]
            
            keyboard_buttons = [
                [InlineKeyboardButton("📊 Get Prices" if lang == 'en' else "📊 Цены", callback_data="price")],
                [InlineKeyboardButton("🔔 Subscribe" if lang == 'en' else "🔔 Подписаться", callback_data="subscribe")]
            ]
            
            if user_id in ADMIN_IDS:
                keyboard_buttons.append([InlineKeyboardButton("🔐 Admin Panel", callback_data="admin_panel")])
            
            keyboard = InlineKeyboardMarkup(keyboard_buttons)
            
            await query.edit_message_text(
                get_text('unsubscribe', lang),
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            last_message_id[chat_id] = query.message.message_id
        
        elif query.data == "targets":
            user_targets = get_user_targets(user_id)
            
            if not user_targets:
                await query.edit_message_text(
                    "🎯 <b>Your Targets</b>\n\n"
                    "You have no active targets.\n\n"
                    "Use /settarget to create a target." if lang == 'en' else
                    "🎯 <b>Ваши цели</b>\n\n"
                    "У вас нет активных целей.\n\n"
                    "Используйте /settarget для создания цели.",
                    parse_mode='HTML'
                )
                return
            
            target_list = []
            for item, price in user_targets.items():
                current = market_data.get(item, {}).get('raw_price', 0)
                status = "✅" if current >= price else "⏳"
                target_list.append(f"{status} {escape_html(item)}: ${price:,} (Current: ${current:,})")
            
            text = (
                "🎯 <b>Your Targets</b>\n\n" if lang == 'en' else "🎯 <b>Ваши цели</b>\n\n"
                + "\n".join(target_list) +
                ("\n\nUse /removetarget <item> to remove" if lang == 'en' else "\n\nИспользуйте /removetarget <item> для удаления")
            )
            
            await query.edit_message_text(text, parse_mode='HTML')
        
        elif query.data == "best_sales":
            if user_id not in ADMIN_IDS:
                await query.edit_message_text("❌ Admin only." if lang == 'en' else "❌ Только для администраторов.")
                return
            
            await context.bot.send_chat_action(chat_id=chat_id, action='typing')
            
            # Получаем все лучшие продажи с +20%
            sales = get_daily_best_all()
            
            if not sales:
                await query.edit_message_text(
                    "No sales data with +20% growth found." if lang == 'en' else "Нет данных о продажах с ростом +20%."
                )
                return
            
            message = "📈 <b>BEST SALES (+20%+ GROWTH)</b>\n\n" if lang == 'en' else "📈 <b>ЛУЧШИЕ ПРОДАЖИ (+20%+ РОСТ)</b>\n\n"
            
            for sale in sales[:20]:
                message += f"📦 {escape_html(sale['item'])}: ${sale['price']:,} (+{sale['change_percent']:.1f}%)\n"
                message += f"   📅 {sale['date']}\n"
            
            if len(sales) > 20:
                message += f"\n... and {len(sales) - 20} more" if lang == 'en' else f"\n... и еще {len(sales) - 20}"
            
            await query.edit_message_text(message, parse_mode='HTML')
        
        elif query.data == "help":
            help_text = get_text('help', lang)
            keyboard_buttons = [
                [InlineKeyboardButton("📊 Get Prices" if lang == 'en' else "📊 Цены", callback_data="price")]
            ]
            
            if user_id in ADMIN_IDS:
                keyboard_buttons.append([InlineKeyboardButton("🔐 Admin Panel", callback_data="admin_panel")])
            
            keyboard = InlineKeyboardMarkup(keyboard_buttons)
            
            await query.edit_message_text(
                help_text,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            last_message_id[chat_id] = query.message.message_id
            
    except Exception as e:
        logger.error(f'Error in button_callback: {e}')
        await query.edit_message_text(get_text('error', lang))

# ============= HELPER FUNCTIONS =============

def get_all_targets() -> Dict[int, Dict[str, int]]:
    return get_all_targets_from_db()

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f'❌ Update {update} caused error {context.error}')
    
    if update and update.effective_chat:
        try:
            user_id = update.effective_user.id if update.effective_user else None
            lang = get_cached_language(user_id) if user_id else 'en'
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=get_text('error', lang)
            )
        except:
            pass

# ============= RECONNECT AND SCHEDULED TASKS =============

async def reconnect_discord():
    global discord_ready, reconnect_attempts, discord_client, discord_connecting
    
    while True:
        if discord_client and not discord_ready and not discord_connecting:
            discord_connecting = True
            reconnect_attempts += 1
            
            if reconnect_attempts > max_reconnect_attempts:
                logger.error(f'❌ Max reconnect attempts ({max_reconnect_attempts}) reached. Waiting 5 minutes...')
                discord_connecting = False
                await asyncio.sleep(300)
                reconnect_attempts = 0
                continue
            
            logger.info(f'🔄 Attempting to reconnect Discord (attempt {reconnect_attempts}/{max_reconnect_attempts})...')
            try:
                if discord_client.is_ready_flag:
                    await discord_client.close()
                    await asyncio.sleep(2)
                await discord_client.start(DISCORD_USER_TOKEN)
                logger.info('✅ Reconnect initiated, waiting for ready...')
                discord_connecting = False
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f'❌ Reconnect failed: {e}')
                discord_connecting = False
                wait_time = min(30, reconnect_attempts * 5)
                logger.info(f'⏳ Waiting {wait_time}s before next attempt...')
                await asyncio.sleep(wait_time)
        
        await asyncio.sleep(10)

async def scheduled_update():
    wait_count = 0
    while not discord_ready:
        wait_count += 1
        if wait_count % 12 == 0:
            logger.info(f'⏳ Waiting for Discord to be ready...')
        await asyncio.sleep(5)
    
    logger.info('✅ Discord ready, starting scheduled updates...')
    
    while True:
        now = datetime.now()
        minutes = now.minute
        seconds = now.second
        micro = now.microsecond
        
        next_minute = ((minutes // 3) + 1) * 3
        if next_minute >= 60:
            next_minute = 0
        
        wait_minutes = next_minute - minutes
        if wait_minutes <= 0:
            wait_minutes += 60
        
        wait_seconds = wait_minutes * 60 - seconds - micro / 1000000
        
        if wait_seconds < 0:
            wait_seconds = 0
        
        logger.info(f"⏰ Next scheduled update in {wait_seconds:.0f} seconds at {next_minute:02d}:00")
        
        await asyncio.sleep(wait_seconds)
        
        try:
            if discord_client and discord_client.is_ready_flag:
                logger.info('🔄 Scheduled update starting...')
                await discord_client.fetch_latest_prices()
                logger.info('✅ Scheduled update complete')
            else:
                logger.warning('⚠️ Discord client not ready for scheduled update')
        except Exception as e:
            logger.error(f'❌ Scheduled update error: {e}')

# ============= MAIN FUNCTION =============

async def main():
    global bot_instance, start_time, discord_client, discord_ready, discord_connecting
    
    start_time = datetime.now()
    
    load_targets()
    
    logger.info('🚀 Starting bot...')
    logger.info(f'📊 Admins: {ADMIN_IDS}')
    logger.info(f'📦 Items: {len(ITEMS_LIST)}')
    logger.info(f'🎯 Targets loaded: {sum(len(t) for t in targets.values())}')
    
    discord_client = DiscordUserClient()
    discord_connecting = True
    
    async def run_discord():
        try:
            await discord_client.start(DISCORD_USER_TOKEN)
        except Exception as e:
            logger.error(f'❌ Discord error: {e}')
            discord_connecting = False
    
    discord_task = asyncio.create_task(run_discord())
    scheduled_task = asyncio.create_task(scheduled_update())
    reconnect_task = asyncio.create_task(reconnect_discord())
    
    application = None
    
    try:
        if USE_PROXY:
            logger.info(f'🔌 Using proxy: {PROXY_HOST}:{PROXY_PORT}')
            os.environ['HTTP_PROXY'] = f'socks5://{PROXY_HOST}:{PROXY_PORT}'
            os.environ['HTTPS_PROXY'] = f'socks5://{PROXY_HOST}:{PROXY_PORT}'
        
        request = HTTPXRequest()
        
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .request(request)
            .build()
        )
        
        bot_instance = application.bot
        
        # Add command handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("price", price_command))
        application.add_handler(CommandHandler("subscribe", subscribe_command))
        application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("admin", admin_menu))
        application.add_handler(CommandHandler("targets", targets_command))
        application.add_handler(CommandHandler("settarget", set_target))
        application.add_handler(CommandHandler("removetarget", remove_target))
        application.add_handler(CommandHandler("cancel", broadcast_message))
        application.add_handler(CommandHandler("broadcast", broadcast_message))
        application.add_handler(CommandHandler("luchse", best_sales_command))
        application.add_handler(CommandHandler("blacklist", admin_blacklist))
        
        # Add callback handlers
        application.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="check_sub"))
        application.add_handler(CallbackQueryHandler(language_selection, pattern="lang_"))
        application.add_handler(CallbackQueryHandler(button_callback))
        
        # Add error handler
        application.add_error_handler(error_handler)
        
        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            timeout=60,
            read_timeout=60,
            write_timeout=60,
            connect_timeout=60,
            pool_timeout=60
        )
        
        logger.info('✅ Bot started successfully!')
        logger.info(f'⏰ Updates scheduled every 3 minutes at 00:00, 00:03, 00:06, etc.')
        logger.info('🌍 Auto-language detection enabled!')
        logger.info('🔥 Best sales tracking with +20% threshold enabled!')
        
        await asyncio.gather(discord_task, scheduled_task, reconnect_task)
        
    except Conflict as e:
        logger.error(f'❌ Bot conflict error: {e}')
        logger.info('⚠️ Another bot instance is running. Stopping this one...')
        if application:
            try:
                await application.updater.stop()
                await application.stop()
            except:
                pass
    except Exception as e:
        logger.error(f'❌ Failed to start bot: {e}')
        import traceback
        traceback.print_exc()
        if application:
            try:
                await application.updater.stop()
                await application.stop()
            except Exception as stop_error:
                logger.error(f'Error stopping application: {stop_error}')
    finally:
        if discord_client:
            try:
                await discord_client.close()
            except Exception as close_error:
                logger.error(f'Error closing discord client: {close_error}')

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('🛑 Bot stopped by user')
    except Exception as e:
        logger.error(f'❌ Fatal error: {e}')
        import traceback
        traceback.print_exc()