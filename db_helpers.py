"""
SQLite persistence layer for Navi MVP
Handles nodes and sessions storage with encryption support
"""

import sqlite3
import json
import os
from datetime import datetime

# Use persistent disk on Render, fallback to local for development
if os.path.exists('/var/data'):
    DB_PATH = '/var/data/navi.db'
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), 'navi.db')

def init_db():
    """Initialize SQLite database with schema and migrate if needed"""
    print(f"[DB] Using database path: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Nodes table - stores saved portal connections
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            portal_name TEXT NOT NULL,
            portal_url TEXT,
            portal_key TEXT,
            node_type TEXT NOT NULL,
            credentials_json TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    
    # Check if portal_key column exists (migration for existing DBs)
    cursor.execute("PRAGMA table_info(nodes)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if 'portal_key' not in columns:
        print("[DB] Migrating nodes table - adding portal_key column")
        cursor.execute('ALTER TABLE nodes ADD COLUMN portal_key TEXT')
        
        # Backfill portal_key for existing nodes
        cursor.execute('SELECT id, portal_name, portal_url FROM nodes')
        existing_nodes = cursor.fetchall()
        
        if existing_nodes:
            from utils import normalize_portal_key
            for node_id, portal_name, portal_url in existing_nodes:
                portal_key = normalize_portal_key(portal_name, portal_url)
                cursor.execute('UPDATE nodes SET portal_key = ? WHERE id = ?', (portal_key, node_id))
            print(f"[DB] Backfilled portal_key for {len(existing_nodes)} existing nodes")
    
    # Sessions table - stores orchestration session snapshots
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            portal_name TEXT,
            portal_url TEXT,
            original_task TEXT,
            session_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    
    conn.commit()
    conn.close()
    print(f"[DB] Initialized database at {DB_PATH}")

def load_saved_nodes():
    """Load all nodes from database into memory"""
    if not os.path.exists(DB_PATH):
        init_db()
        return {}
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('SELECT id, type, portal_name, portal_url, portal_key, node_type, credentials_json, metadata_json FROM nodes')
    rows = cursor.fetchall()
    
    nodes = {}
    for row in rows:
        node_id, node_type, portal_name, portal_url, portal_key, node_type_field, creds_json, meta_json = row
        
        # Backfill portal_key if missing (shouldn't happen after migration, but defensive)
        if not portal_key:
            from utils import normalize_portal_key
            portal_key = normalize_portal_key(portal_name, portal_url)
        
        nodes[node_id] = {
            "type": node_type,
            "portal_name": portal_name,
            "portal_url": portal_url,
            "portal_key": portal_key,
            "node_type": node_type_field,
            "credentials": json.loads(creds_json) if creds_json else {},
            "metadata": json.loads(meta_json) if meta_json else {}
        }
    
    conn.close()
    print(f"[DB] Loaded {len(nodes)} nodes from database")
    return nodes

def persist_node(node_id, node_data):
    """Save or update a node in the database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    now = datetime.now().isoformat()
    
    # Check if node exists
    cursor.execute('SELECT id FROM nodes WHERE id = ?', (node_id,))
    exists = cursor.fetchone()
    
    creds_json = json.dumps(node_data.get("credentials", {}))
    meta_json = json.dumps(node_data.get("metadata", {}))
    
    # Debug logging
    print(f"[DB] persist_node - node_data keys: {list(node_data.keys())}")
    
    if exists:
        cursor.execute('''
            UPDATE nodes 
            SET type = ?, portal_name = ?, portal_url = ?, portal_key = ?, node_type = ?, 
                credentials_json = ?, metadata_json = ?, updated_at = ?
            WHERE id = ?
        ''', (
            node_data.get("type", "browser"),
            node_data.get("portal_name", ""),
            node_data.get("portal_url", ""),
            node_data.get("portal_key", ""),
            node_data.get("node_type", "browser"),
            creds_json,
            meta_json,
            now,
            node_id
        ))
        print(f"[DB] Updated node {node_id}")
    else:
        # INSERT: 10 columns require 10 values
        values = (
            node_id,
            node_data.get("type", "browser"),
            node_data.get("portal_name", ""),
            node_data.get("portal_url", ""),
            node_data.get("portal_key", ""),
            node_data.get("node_type", "browser"),
            creds_json,
            meta_json,
            now,
            now
        )
        print(f"[DB] Inserting node - 10 columns, {len(values)} values")
        
        cursor.execute('''
            INSERT INTO nodes (id, type, portal_name, portal_url, portal_key, node_type, credentials_json, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', values)
        print(f"[DB] Created node {node_id}")
    
    conn.commit()
    conn.close()

def persist_session(session_id, session_data):
    """Save or update a session snapshot in the database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    now = datetime.now().isoformat()
    
    # Check if session exists
    cursor.execute('SELECT id FROM sessions WHERE id = ?', (session_id,))
    exists = cursor.fetchone()
    
    session_json = json.dumps(session_data)
    
    if exists:
        cursor.execute('''
            UPDATE sessions 
            SET portal_name = ?, portal_url = ?, original_task = ?, session_json = ?, updated_at = ?
            WHERE id = ?
        ''', (
            session_data.get("portal_name", ""),
            session_data.get("portal_url", ""),
            session_data.get("original_task", ""),
            session_json,
            now,
            session_id
        ))
    else:
        cursor.execute('''
            INSERT INTO sessions (id, portal_name, portal_url, original_task, session_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            session_id,
            session_data.get("portal_name", ""),
            session_data.get("portal_url", ""),
            session_data.get("original_task", ""),
            session_json,
            now,
            now
        ))
    
    conn.commit()
    conn.close()

def load_saved_sessions():
    """Load all sessions from database into memory"""
    if not os.path.exists(DB_PATH):
        init_db()
        return {}
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('SELECT id, session_json FROM sessions')
    rows = cursor.fetchall()
    
    sessions = {}
    for row in rows:
        session_id, session_json = row
        sessions[session_id] = json.loads(session_json) if session_json else {}
    
    conn.close()
    print(f"[DB] Loaded {len(sessions)} sessions from database")
    return sessions

def delete_session(session_id):
    """Delete a session from the database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
    conn.commit()
    conn.close()
    print(f"[DB] Deleted session {session_id}")

def delete_node(node_id):
    """Delete a node from the database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM nodes WHERE id = ?', (node_id,))
    conn.commit()
    conn.close()
    print(f"[DB] Deleted node {node_id}")

def reset_all_state():
    """Clear all in-memory and SQLite state for clean testing"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Delete all nodes
    cursor.execute('DELETE FROM nodes')
    nodes_deleted = cursor.rowcount
    
    # Delete all sessions
    cursor.execute('DELETE FROM sessions')
    sessions_deleted = cursor.rowcount
    
    conn.commit()
    conn.close()
    
    print(f"[DB RESET] Deleted {nodes_deleted} nodes and {sessions_deleted} sessions from database")
    
    return {
        "nodes_deleted": nodes_deleted,
        "sessions_deleted": sessions_deleted
    }
