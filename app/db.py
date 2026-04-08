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
            resolution TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_ticket(ticket):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT OR IGNORE INTO tickets 
        (id, title, description, submitter, timestamp, status, category, resolution)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        ticket.id, ticket.title, ticket.description,
        ticket.submitter, ticket.timestamp,
        ticket.status, getattr(ticket, 'category', None), None
    ))
    conn.commit()
    conn.close()

def update_ticket(ticket_id, status, category=None, resolution=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE tickets SET status=?, category=?, resolution=?
        WHERE id=?
    ''', (status, category, resolution, ticket_id))
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