# Start the browser chat server with `make start`.
#
# Resolve the actual Python command from the shell's `py` alias/command
# (`type py` shows it). Make recipes run under /bin/sh and do not inherit
# interactive aliases, so ask a login bash and extract the command. If `py`
# is not defined, fall back to python3.

PYTHON ?= $(shell \
	/bin/bash -lc 'shopt -s expand_aliases; type py' 2>/dev/null | \
	sed -n -E "s/^py is aliased to [\`'](.*)'$$/\1/p; s/^py is (\/.*)$$/\1/p; s/^py is (.*)$$/\1/p" | \
	head -n 1)

ifeq ($(strip $(PYTHON)),)
PYTHON := $(shell command -v python3 2>/dev/null || echo python3)
endif

HOST ?= 127.0.0.1
PORT ?= 8765

.PHONY: start

start:
	$(PYTHON) app/chat_server.py --host $(HOST) --port $(PORT)
