#!/bin/zsh
cd "$(dirname "$0")"
exec .venv/bin/uvicorn app.main:app --reload --port 8010
