# Install plaud-fetch into its own venv that sees the Debian packages
# (python3-bleak, python3-cryptography, python3-numpy), and link the command
# into ~/.local/bin.

BRIDGE ?= $(HOME)/dev/plaud-direct-pc-research/pc-bridge
VENV   ?= $(HOME)/.local/share/plaud-fetch/venv
BINDIR ?= $(HOME)/.local/bin

.PHONY: install test uninstall

install:
	python3 -m venv --system-site-packages $(VENV)
	# The bridge pins cryptography>=45; Debian's 43 has every primitive it uses.
	$(VENV)/bin/pip install --quiet --no-deps -e $(BRIDGE)
	$(VENV)/bin/pip install --quiet --no-deps -e .
	mkdir -p $(BINDIR)
	ln -sf $(VENV)/bin/plaud-fetch $(BINDIR)/plaud-fetch
	@$(BINDIR)/plaud-fetch --version

test:
	$(VENV)/bin/python -m pytest -q

uninstall:
	rm -f $(BINDIR)/plaud-fetch
	rm -rf $(VENV)
