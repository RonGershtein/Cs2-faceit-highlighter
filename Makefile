# CS2 FACEIT Highlighter — common commands.
#   make install   create the Python env, install dependencies, download radar images
#   make run       start the app (http://localhost:8000)
#   make tools     download HLAE + FFmpeg for recording (Windows only)
#   make doctor    check Python, dependencies, CS2 path, tools, config
#   make test      run unit tests
#   make parse DEMO=path/to/match.dem.zst   parse one demo from the CLI
#   make update    git pull + reinstall dependencies
#   make shortcut  put a "CS2 Highlighter" icon on the Desktop (Windows)
#   make clean     remove the Python env and caches (keeps your data)
# On Windows without make, use:  cs2w.bat <same command>

ifeq ($(OS),Windows_NT)
  PY      ?= py -3.12
  VENV_PY := .venv/Scripts/python.exe
  RM_RF   := rmdir /s /q
else
  PY      ?= python3
  VENV_PY := .venv/bin/python
  RM_RF   := rm -rf
endif

.PHONY: help install run dev tools doctor test parse update clean shortcut

help:
	@echo "make install | run | tools | doctor | test | parse DEMO=file | update | clean"

$(VENV_PY):
	$(PY) -m venv .venv || python -m venv .venv

install: $(VENV_PY)
	$(VENV_PY) -m pip install --upgrade pip -q
	$(VENV_PY) -m pip install -r requirements.txt -q
	$(VENV_PY) scripts/fetch_radars.py
	@echo "installed. next: make run"

run: $(VENV_PY)
	$(VENV_PY) run.py

dev: $(VENV_PY)
	$(VENV_PY) -m uvicorn backend.app:app --reload --port 8000

tools: $(VENV_PY)
	$(VENV_PY) scripts/fetch_tools.py

doctor: $(VENV_PY)
	$(VENV_PY) scripts/doctor.py

test: $(VENV_PY)
	$(VENV_PY) -m pytest -q tests/test_highlights.py tests/test_record.py

parse: $(VENV_PY)
	$(VENV_PY) -m backend.ingest "$(DEMO)"

update:
	git pull --ff-only
	$(MAKE) install

shortcut:
	powershell -NoProfile -ExecutionPolicy Bypass -File scripts/make_shortcut.ps1

clean:
	-$(RM_RF) .venv
	-$(RM_RF) .pytest_cache
