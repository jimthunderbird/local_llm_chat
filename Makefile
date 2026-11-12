# Start the browser chat server with `make start`.
#
# Some machines expose a pyenv/shim `python3` that does not have the project
# requirements installed, so prefer the first interpreter that can import
# requests. Override explicitly with `make start PYTHON=/path/to/python3`.

PYTHON ?= $(shell \
	for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do \
		if command -v $$candidate >/dev/null 2>&1 && \
		   $$candidate -c 'import requests' >/dev/null 2>&1; then \
			echo $$candidate; exit 0; \
		fi; \
	done; \
	echo python3)
HOST ?= 127.0.0.1
PORT ?= 8765

.PHONY: start

start:
	$(PYTHON) app/chat_server.py --host $(HOST) --port $(PORT)
