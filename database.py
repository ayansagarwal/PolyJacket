"""
Database module for PolyJacket prediction market
Handles SQLite storage for users, markets, and positions
"""

import sqlite3
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from pathlib import Path
import json

DATABASE_FILE = Path("data/polyjacket.db")

def init_database():
    """Initialize database tables"""
    DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    # Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            hashed_password TEXT NOT NULL,
            balance REAL DEFAULT 10000,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    """)
    
    # Markets table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            market_id TEXT PRIMARY KEY,
            game_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            sport TEXT NOT NULL,
            game_time TEXT NOT NULL,
            game_date TEXT NOT NULL,
            status TEXT NOT NULL,
            home_price REAL NOT NULL,
            away_price REAL NOT NULL,
            home_shares REAL NOT NULL,
            away_shares REAL NOT NULL,
            total_volume REAL NOT NULL,
            winner TEXT,
            home_score TEXT,
            away_score TEXT,
            home_elo REAL,
            away_elo REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            settled_at TIMESTAMP
        )
    """)
    
    # Positions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            market_id TEXT NOT NULL,
            home_shares REAL DEFAULT 0,
            away_shares REAL DEFAULT 0,
            avg_home_price REAL DEFAULT 0,
            avg_away_price REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (market_id) REFERENCES markets(market_id),
            UNIQUE(user_id, market_id)
        )
    """)
    
    # Price history table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            home_price REAL NOT NULL,
            away_price REAL NOT NULL,
            home_shares REAL NOT NULL,
            away_shares REAL NOT NULL,
            total_volume REAL NOT NULL,
            FOREIGN KEY (market_id) REFERENCES markets(market_id)
        )
    """)

    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_history_market ON price_history(market_id, timestamp)")
    
    conn.commit()
    conn.close()
    print(f"[OK] Database initialized at {DATABASE_FILE}")


def get_connection():
    """Get database connection"""
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row  # Enable column access by name
    return conn


# ============== USER OPERATIONS ==============

def create_user(username: str, email: str, hashed_password: str, starting_balance: float = 10000) -> Optional[int]:
    """Create new user, returns user_id or None if username/email exists"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (username, email, hashed_password, balance) VALUES (?, ?, ?, ?)",
            (username, email, hashed_password, starting_balance)
        )
        user_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return user_id
    except sqlite3.IntegrityError:
        return None


def get_user_by_username(username: str) -> Optional[Dict]:
    """Get user by username"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return dict(row)
    return None


def get_user_by_id(user_id: int) -> Optional[Dict]:
    """Get user by ID"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return dict(row)
    return None


def update_user_balance(user_id: int, new_balance: float):
    """Update user's balance"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user_id))
    conn.commit()
    conn.close()


def update_last_login(user_id: int):
    """Update user's last login timestamp"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


# ============== MARKET OPERATIONS ==============

def upsert_market(market: Dict):
    """Insert or update a market"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO markets (
            market_id, game_id, home_team, away_team, sport, game_time, game_date,
            status, home_price, away_price, home_shares, away_shares, total_volume,
            winner, home_score, away_score, home_elo, away_elo
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
            status = excluded.status,
            home_price = excluded.home_price,
            away_price = excluded.away_price,
            home_shares = excluded.home_shares,
            away_shares = excluded.away_shares,
            total_volume = excluded.total_volume,
            winner = excluded.winner,
            home_score = excluded.home_score,
            away_score = excluded.away_score,
            settled_at = CASE WHEN excluded.status = 'settled' THEN CURRENT_TIMESTAMP ELSE settled_at END
    """, (
        market["market_id"], market["game_id"], market["home_team"], market["away_team"],
        market["sport"], market["game_time"], market["game_date"], market["status"],
        market["home_price"], market["away_price"], market["home_shares"], market["away_shares"],
        market["total_volume"], market.get("winner"), market.get("home_score"), 
        market.get("away_score"), market.get("home_elo"), market.get("away_elo")
    ))
    
    conn.commit()
    conn.close()


def get_market(market_id: str) -> Optional[Dict]:
    """Get market by ID"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM markets WHERE market_id = ?", (market_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return dict(row)
    return None


def get_all_markets() -> List[Dict]:
    """Get all markets"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM markets ORDER BY game_date DESC, game_time DESC")
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_markets_by_status(status: str) -> List[Dict]:
    """Get markets by status"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM markets WHERE status = ? ORDER BY game_date DESC, game_time DESC", (status,))
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


# ============== POSITION OPERATIONS ==============

def upsert_position(user_id: int, market_id: str, home_shares: float = 0, away_shares: float = 0,
                   avg_home_price: float = 0, avg_away_price: float = 0):
    """Insert or update a position"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO positions (user_id, market_id, home_shares, away_shares, avg_home_price, avg_away_price, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, market_id) DO UPDATE SET
            home_shares = excluded.home_shares,
            away_shares = excluded.away_shares,
            avg_home_price = excluded.avg_home_price,
            avg_away_price = excluded.avg_away_price,
            updated_at = CURRENT_TIMESTAMP
    """, (user_id, market_id, home_shares, away_shares, avg_home_price, avg_away_price))
    
    conn.commit()
    conn.close()


def get_user_positions(user_id: int) -> List[Dict]:
    """Get all positions for a user"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.*, m.* 
        FROM positions p
        JOIN markets m ON p.market_id = m.market_id
        WHERE p.user_id = ?
        AND (p.home_shares > 0 OR p.away_shares > 0)
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_position(user_id: int, market_id: str) -> Optional[Dict]:
    """Get a specific position"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM positions WHERE user_id = ? AND market_id = ?", (user_id, market_id))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return dict(row)
    return None


def delete_empty_positions(user_id: int):
    """Delete positions with no shares"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM positions 
        WHERE user_id = ? AND home_shares = 0 AND away_shares = 0
    """, (user_id,))
    conn.commit()
    conn.close()


# ============== STATS ==============

def get_user_count() -> int:
    """Get total number of users"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM users")
    result = cursor.fetchone()
    conn.close()
    return result['count'] if result else 0


def get_market_count() -> int:
    """Get total number of markets"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM markets")
    result = cursor.fetchone()
    conn.close()
    return result['count'] if result else 0


# ============== PRICE HISTORY ==============

def record_price_snapshot(market_id: str, home_price: float, away_price: float,
                          home_shares: float, away_shares: float, total_volume: float):
    """Record a price snapshot after a trade"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO price_history (market_id, home_price, away_price, home_shares, away_shares, total_volume)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (market_id, home_price, away_price, home_shares, away_shares, total_volume))
    conn.commit()
    conn.close()


def get_price_history(market_id: str) -> List[Dict]:
    """Get price history for a market, ordered chronologically"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, market_id, timestamp, home_price, away_price, home_shares, away_shares, total_volume
        FROM price_history
        WHERE market_id = ?
        ORDER BY timestamp ASC
    """, (market_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]
