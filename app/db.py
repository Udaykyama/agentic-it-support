import sqlite3
from datetime import datetime, timedelta

DB_PATH = "tickets.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            submitter TEXT,
            timestamp TEXT,
            status TEXT,
            category TEXT,
            resolution TEXT,
            confidence REAL
        )
    ''')
    # Add confidence column if it doesn't exist (migration for existing DBs)
    try:
        c.execute("ALTER TABLE tickets ADD COLUMN confidence REAL")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()
    conn.close()


def save_ticket(ticket):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT OR IGNORE INTO tickets 
        (id, title, description, submitter, timestamp, status, category, resolution, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        ticket.id, ticket.title, ticket.description,
        ticket.submitter, ticket.timestamp,
        ticket.status, getattr(ticket, 'category', None), None, None
    ))
    conn.commit()
    conn.close()


def update_ticket(ticket_id, status, category=None, resolution=None, confidence=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE tickets SET status=?, category=?, resolution=?, confidence=?
        WHERE id=?
    ''', (status, category, resolution, confidence, ticket_id))
    conn.commit()
    conn.close()


def is_duplicate(ticket):
    return False


def get_tickets_by_category(category, limit=50):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT id, title, description, resolution FROM tickets
        WHERE category=? ORDER BY timestamp DESC LIMIT ?
    ''', (category, limit))
    rows = c.fetchall()
    conn.close()
    return rows


def get_all_tickets(limit=100):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT id, title, submitter, timestamp, status, category, confidence
        FROM tickets ORDER BY timestamp DESC LIMIT ?
    ''', (limit,))
    rows = c.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "title": r[1],
            "submitter": r[2],
            "timestamp": r[3],
            "status": r[4],
            "category": r[5],
            "confidence": r[6],
        }
        for r in rows
    ]
