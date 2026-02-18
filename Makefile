.PHONY: dev build install test clean help

help:
	@echo "Content Posting Lab - Development Commands"
	@echo ""
	@echo "  make install    Install Python and Node dependencies"
	@echo "  make dev        Start uvicorn + Vite dev server (concurrent)"
	@echo "  make build      Build frontend for production"
	@echo "  make test       Run tests"
	@echo "  make clean      Remove build artifacts and cache"

install:
	pip install -r requirements.txt
	cd frontend && npm install

dev:
	@command -v concurrently >/dev/null 2>&1 || npm install -g concurrently
	concurrently \
		"python -m uvicorn app:app --reload --port 8000" \
		"cd frontend && npm run dev"

build:
	cd frontend && npm run build

test:
	@echo "Running pytest (backend)..."
	pytest tests/test_smoke.py -v
	@echo ""
	@echo "Running vitest (frontend unit tests)..."
	cd frontend && npm run test
	@echo ""
	@echo "✓ Unit tests passed!"
	@echo ""
	@echo "Note: Run 'make test-e2e' to run Playwright e2e tests (requires servers running)"

test-e2e:
	@echo "Running Playwright (e2e tests)..."
	@echo "Make sure both servers are running:"
	@echo "  - Backend: python -m uvicorn app:app --port 8000"
	@echo "  - Frontend: cd frontend && npm run dev"
	cd frontend && npx playwright test

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache
	rm -rf frontend/dist frontend/node_modules
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
