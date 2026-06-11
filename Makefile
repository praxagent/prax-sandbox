.PHONY: lint test ci build

lint:
	uvx ruff@latest check .

test:
	uv run --python 3.13 pytest tests/ -q

ci: lint test
	@echo "\nAll prax-sandbox checks passed."

# Build the sandbox image (OpenCode + Chrome/CDP + desktop).
build:
	docker build -t prax-sandbox:latest sandbox/
