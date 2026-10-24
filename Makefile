PY := .venv/bin/python

.PHONY: install check test lint graph security

install:
	python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

check:
	.venv/bin/ruff check . && $(PY) -m pytest -q

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check .

# Граф кода для graphify (локально, tree-sitter, без LLM): graphify-out/
graph:
	graphify update .

# Быстрый поиск уязвимостей в коде через strix: нужны Docker и STRIX_LLM + LLM_API_KEY в окружении
security:
	strix -n --target ./ --scan-mode quick
