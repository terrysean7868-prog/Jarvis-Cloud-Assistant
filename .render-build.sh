#!/usr/bin/env bash
set -euo pipefail

echo "🪶 Installing dependencies"
pip install --upgrade pip
if [ -f requirements.render.txt ]; then
	echo "🪶 Using requirements.render.txt"
	pip install -r requirements.render.txt
else
	pip install -r requirements.txt
fi

echo "🪶 Building frontend (jarvis-frontend)"
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
	echo "🪶 Node: $(node --version)"
	echo "🪶 NPM:  $(npm --version)"
	cd jarvis-frontend
	# Force a clean build output so we never serve stale assets.
	rm -rf build
	# Prefer reproducible installs on Render
	if [ -f package-lock.json ]; then
		if ! npm ci; then
			echo "⚠️  npm ci failed (lockfile out of sync). Falling back to npm install."
			npm install
		fi
	else
		npm install
	fi
	npm run build
	cd ..
else
	echo "⚠️  Node/npm not found in this environment."
	echo "⚠️  Frontend build will be skipped, and the UI may not be served unless jarvis-frontend/build is present in the repo."
fi

if [ ! -f jarvis-frontend/build/index.html ]; then
	echo "❌ Frontend build missing: jarvis-frontend/build/index.html"
	echo "❌ This would cause a blank or missing UI in production. Failing the build."
	exit 1
fi

echo "✅ Frontend build present"
