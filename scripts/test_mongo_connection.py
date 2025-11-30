"""
Simple test script to verify MongoDB (Atlas) connection using the project's `.env`.
Usage:
    python scripts/test_mongo_connection.py

It reads `MONGODB_URI` from environment or `.env` and attempts a ping and lists databases.
"""
import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

URI = os.getenv('MONGODB_URI')
if not URI:
    print("MONGODB_URI not set in environment. Please set it in .env or the shell.")
    raise SystemExit(1)

print("Testing MongoDB URI:", URI if os.getenv('SHOW_MONGO_URI', 'false').lower() in ('1','true') else URI.split('@')[-1])

try:
    client = MongoClient(URI, serverSelectionTimeoutMS=5000)
    print('Ping:', client.admin.command('ping'))
    print('Databases:', client.list_database_names())
    print('OK - MongoDB connection successful')
except Exception as e:
    print('ERROR connecting to MongoDB:', type(e).__name__, e)
    raise
