#!/usr/bin/env bash
echo "🪶 Installing dependencies"
pip install --upgrade pip
if [ -f requirements.render.txt ]; then
	echo "🪶 Using requirements.render.txt"
	pip install -r requirements.render.txt
else
	pip install -r requirements.txt
fi
