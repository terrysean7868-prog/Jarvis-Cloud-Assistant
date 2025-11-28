#!/usr/bin/env python
"""Install all required dependencies"""
import subprocess
import sys

packages = [
    'fastapi>=0.104.0',
    'uvicorn[standard]>=0.24.0',
    'aiohttp>=3.8.5',
    'pydantic>=2.0.0',
    'python-dotenv>=1.0.0',
    'requests>=2.31.0',
    'pymongo>=4.4.0',
    'openai>=1.0.0',
    'gitpython>=3.1.0',
]

print("Installing critical packages...")
for package in packages:
    print(f"  Installing {package}...", end=' ', flush=True)
    result = subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', package], capture_output=True)
    if result.returncode == 0:
        print("OK")
    else:
        print(f"FAILED: {result.stderr.decode()}")

print("\nInstalling full requirements.txt...")
result = subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-r', 'requirements.txt'], capture_output=True)
if result.returncode == 0:
    print("All packages installed successfully!")
else:
    print(f"Some packages failed to install: {result.stderr.decode()}")
    print("But critical packages should be working.")

sys.exit(0)
