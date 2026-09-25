#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f config.yml ]]; then echo 'Missing config.yml. Define repositories and GitHub Projects first.' >&2; exit 1; fi
if ! command -v python3 >/dev/null 2>&1; then echo 'Python 3 is required.' >&2; exit 1; fi
if ! command -v gh >/dev/null 2>&1; then echo 'GitHub CLI (gh) is required. Install it and run gh auth login.' >&2; exit 1; fi
if ! gh auth status >/dev/null 2>&1; then echo 'GitHub CLI is not authenticated. Run gh auth login.' >&2; exit 1; fi
if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
if [[ ! -f .venv/.requirements.sha256 ]] || [[ "$(sha256sum requirements.txt | cut -d' ' -f1)" != "$(cat .venv/.requirements.sha256)" ]]; then
  .venv/bin/python -m pip install -r requirements.txt
  sha256sum requirements.txt | cut -d' ' -f1 > .venv/.requirements.sha256
fi
exec .venv/bin/python -m app.launcher
