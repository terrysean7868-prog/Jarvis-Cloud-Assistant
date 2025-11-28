#!/usr/bin/env python
"""Test the bot health endpoint"""
import time
import requests

# Give server time to start
time.sleep(2)

try:
    resp = requests.get('http://localhost:8000/health', timeout=5)
    print(f"[TEST] Status Code: {resp.status_code}")
    print(f"[TEST] Response: {resp.json()}")
    if resp.status_code == 200:
        print("\n[SUCCESS] Bot is running and responding to health checks!")
    else:
        print(f"\n[ERROR] Unexpected status code: {resp.status_code}")
except requests.ConnectionError as e:
    print(f"[ERROR] Could not connect to bot on http://localhost:8000: {e}")
except Exception as e:
    print(f"[ERROR] Test failed: {e}")
