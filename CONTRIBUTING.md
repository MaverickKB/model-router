# Contributing

Issues and pull requests are welcome.

Before opening a pull request:

1. Keep provider URLs, credentials and runtime state in local configuration.
2. Run `uv run python -m pytest -q` and `npm run test:ui`.
3. Run `npm run typecheck`, `npm run build` and `uv run ruff check gateway tests`.
4. Explain the user-visible behavior and the evidence used to verify it.

Keep changes focused on the router's general contracts. New integrations should
be configurable adapters rather than project-specific routes or model names.
