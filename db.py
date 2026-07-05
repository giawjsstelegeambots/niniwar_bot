# database/db.py
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Set
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data.db')

# Пул соединений (простой, но эффективный)
_connections_pool = []
_MAX_POOL_SIZE = 5

@contextmanager
def get_db_connection():
    """Контекстный менеджер для соединения с БД с пулом"""
    conn = None
    
    # Пытаемся взять соединение из пула
    if _connections_pool:
        conn = _connections_pool.pop()
        try:
            # Проверяем, живо ли соединение
            conn.cursor().execute('SELECT 1')
        except:
            conn = None
    
    # Если нет соединения в пуле или оно мертво - создаем новое
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    
    try:
        yield conn
    finally:
        # Возвращаем соединение в пул, если он не переполнен
        if len(_connections_pool) < _MAX_POOL_SIZE:
            _connections_pool.append(conn)
        else:
            conn.close()

def get_db():
    """Для обратной совместимости"""
    return get_db_connection().__enter__()

def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                language TEXT DEFAULT 'en',
                subscribed INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT,
                price INTEGER,
                change TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_best_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT,
                price INTEGER,
                change_percent REAL,
                date DATE,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                item TEXT,
                target_price INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        ''')
        
        # Индексы для ускорения запросов
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_price_history_item ON price_history(item)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_price_history_timestamp ON price_history(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_daily_best_date ON daily_best_sales(date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_daily_best_item ON daily_best_sales(item)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_targets_user ON targets(user_id)')
        
        conn.commit()

def get_user(user_id: int) -> Optional[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = cursor.fetchone()
        return dict(user) if user else None

def create_user(user_id: int, username: str = None, first_name: str = None):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
        ''', (user_id, username, first_name))
        conn.commit()

def set_user_language(user_id: int, language: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET language = ? WHERE user_id = ?', (language, user_id))
        conn.commit()

def get_user_language(user_id: int) -> str:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT language FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result['language'] if result else 'en'

def add_subscriber(user_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO subscribers (user_id) VALUES (?)', (user_id,))
        cursor.execute('UPDATE users SET subscribed = 1 WHERE user_id = ?', (user_id,))
        conn.commit()

def remove_subscriber(user_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM subscribers WHERE user_id = ?', (user_id,))
        cursor.execute('UPDATE users SET subscribed = 0 WHERE user_id = ?', (user_id,))
        conn.commit()

def is_subscribed(user_id: int) -> bool:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM subscribers WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return bool(result)

def add_to_blacklist(user_id: int, reason: str = None):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO blacklist (user_id, reason) VALUES (?, ?)', (user_id, reason))
        cursor.execute('UPDATE users SET is_banned = 1 WHERE user_id = ?', (user_id,))
        cursor.execute('DELETE FROM subscribers WHERE user_id = ?', (user_id,))
        conn.commit()

def remove_from_blacklist(user_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM blacklist WHERE user_id = ?', (user_id,))
        cursor.execute('UPDATE users SET is_banned = 0 WHERE user_id = ?', (user_id,))
        conn.commit()

def is_banned(user_id: int) -> bool:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM blacklist WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return bool(result)

def save_price_history(item: str, price: int, change: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO price_history (item, price, change) VALUES (?, ?, ?)', (item, price, change))
        conn.commit()

def save_price_history_batch(items: List[tuple]):
    """Массовая вставка истории цен"""
    if not items:
        return
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany('INSERT INTO price_history (item, price, change) VALUES (?, ?, ?)', items)
        conn.commit()

def save_daily_best(item: str, price: int, change_percent: float):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        today = datetime.now().date().isoformat()
        cursor.execute('''
            INSERT INTO daily_best_sales (item, price, change_percent, date)
            VALUES (?, ?, ?, ?)
        ''', (item, price, change_percent, today))
        conn.commit()

def save_daily_best_batch(items: List[tuple]):
    """Массовая вставка лучших продаж"""
    if not items:
        return
    with get_db_connection() as conn:
        cursor = conn.cursor()
        today = datetime.now().date().isoformat()
        data = [(item, price, change_percent, today) for item, price, change_percent in items]
        cursor.executemany('''
            INSERT INTO daily_best_sales (item, price, change_percent, date)
            VALUES (?, ?, ?, ?)
        ''', data)
        conn.commit()

def get_daily_best(date: str = None) -> List[Dict]:
    if not date:
        date = datetime.now().date().isoformat()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM daily_best_sales 
            WHERE date = ? AND change_percent >= 20
            ORDER BY change_percent DESC
        ''', (date,))
        results = cursor.fetchall()
        return [dict(row) for row in results]

def get_daily_best_all() -> List[Dict]:
    """Получить все лучшие продажи с +20% за все время"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM daily_best_sales 
            WHERE change_percent >= 20
            ORDER BY change_percent DESC, date DESC
            LIMIT 100
        ''')
        results = cursor.fetchall()
        return [dict(row) for row in results]

def save_target(user_id: int, item: str, target_price: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO targets (user_id, item, target_price)
            VALUES (?, ?, ?)
        ''', (user_id, item, target_price))
        conn.commit()

def get_user_targets(user_id: int) -> Dict[str, int]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT item, target_price FROM targets WHERE user_id = ?', (user_id,))
        results = cursor.fetchall()
        return {row['item']: row['target_price'] for row in results}

def get_all_targets_from_db() -> Dict[int, Dict[str, int]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, item, target_price FROM targets')
        results = cursor.fetchall()
    
    all_targets = {}
    for row in results:
        if row['user_id'] not in all_targets:
            all_targets[row['user_id']] = {}
        all_targets[row['user_id']][row['item']] = row['target_price']
    
    return all_targets

def remove_target(user_id: int, item: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM targets WHERE user_id = ? AND item = ?', (user_id, item))
        conn.commit()

def get_all_subscribers() -> Set[int]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM subscribers')
        results = cursor.fetchall()
        return {row['user_id'] for row in results}