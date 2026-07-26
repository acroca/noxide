.PHONY: auth run test lint

# Local development. Deployment is documented, not shipped — the Compose files
# live in docs/deployment.md for users to copy.

auth:
	uv run assistant auth

run:
	uv run assistant run

test:
	uv run pytest tests/ -v

lint:
	uv run ruff check src/ tests/
