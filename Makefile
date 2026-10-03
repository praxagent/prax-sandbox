.PHONY: lint test ci build image

lint:
	uvx ruff@latest check .

test:
	uv run --python 3.13 --extra daemon pytest tests/ -q

ci: lint test
	@echo "\nAll prax-sandbox checks passed."

# Build the sandbox image (Python/scientific stack + Chrome/CDP + desktop),
# with your own packages from sandbox/local-packages.txt.
build:
	scripts/ensure-image.sh --rebuild

# Build only if the image is missing or sandbox/local-packages.txt changed.
image:
	scripts/ensure-image.sh
