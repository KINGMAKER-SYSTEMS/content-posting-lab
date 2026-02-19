.PHONY: help install dev build start test test-e2e clean

help:
	@echo "Content Posting Lab - Development Commands"
	@echo ""
	@echo "  make install    Install Python/Node deps and Playwright browser"
	@echo "  make dev        Start API + Vite with labeled output"
	@echo "  make build      Build frontend for production"
	@echo "  make start      Run unified FastAPI app on port 8000"
	@echo "  make test       Run backend + unit + e2e test pipeline"
	@echo "  make test-e2e   Run Playwright tests from frontend/"
	@echo "  make clean      Remove local build and cache artifacts"

install:
	pip install -r requirements.txt
	cd frontend && npm install && npx playwright install chromium

dev:
	npx concurrently -n api,ui -c cyan,magenta "python -m uvicorn app:app --reload --port 8000" "npm --prefix frontend run dev"

build:
	npm --prefix frontend run build

start:
	python app.py

test:
	pytest tests -q
	npm --prefix frontend run test
	npm --prefix frontend run test:e2e

test-e2e:
	npm --prefix frontend run test:e2e

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache
	rm -rf frontend/dist frontend/node_modules
