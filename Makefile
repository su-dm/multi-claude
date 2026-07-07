# multi-claude build/install targets. Runtime deps: python3 (>=3.10), tmux.

PREFIX ?= $(HOME)/.local
BIN    := $(PREFIX)/bin

.PHONY: help test smoke install uninstall check run

help:
	@echo "targets: test (unit) · smoke (tmux integration) · install · uninstall · check (all tests) · run"

test:
	python3 -m unittest discover -s tests -v

smoke:
	bash tests/smoke.sh

check: test smoke

install:
	@command -v tmux >/dev/null || { echo "error: tmux is required (sudo apt install tmux)"; exit 1; }
	@mkdir -p $(BIN)
	ln -sf $(CURDIR)/bin/multi-claude $(BIN)/multi-claude
	@echo "installed: $(BIN)/multi-claude (symlink into this repo)"
	@case ":$$PATH:" in *:"$(BIN)":*) ;; *) echo "NOTE: $(BIN) is not on your PATH";; esac

uninstall:
	rm -f $(BIN)/multi-claude

run:
	./bin/multi-claude
