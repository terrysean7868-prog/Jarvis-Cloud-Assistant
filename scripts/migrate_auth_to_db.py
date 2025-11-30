#!/usr/bin/env python
"""Migrate local auth_users.json to MongoDB auth_users collection.
Usage: cd project && venv\Scripts\python scripts\migrate_auth_to_db.py
"""
import json
from pathlib import Path
from src.utils.db import db
import os

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTH_FILE = PROJECT_ROOT / "data" / "auth_users.json"

if not AUTH_FILE.exists():
    print("No local auth file found at", AUTH_FILE)
    raise SystemExit(1)

with open(AUTH_FILE, 'r', encoding='utf-8') as f:
    data = json.load(f)

users = data.get('users', {})
if not users:
    print('No users to migrate.')
    raise SystemExit(0)

print('Connecting to MongoDB...')
try:
    db._ensure_connected()
except Exception as e:
    print('Failed to connect to MongoDB:', e)
    raise SystemExit(1)

col = db.db.auth_users
col.create_index([('username', 1)], unique=True)
count = 0
for uname, u in users.items():
    doc = dict(u)
    doc['username'] = uname
    try:
        col.update_one({'username': uname}, {'$set': doc}, upsert=True)
        count += 1
    except Exception as e:
        print('Failed to migrate', uname, e)

print(f'Migrated {count} users to MongoDB auth_users collection.')
