#!/bin/sh
# All static checks, as run by the pre-commit hook and CI:
#   lint + format (ruff), types (mypy), shell scripts (shellcheck), spelling (codespell),
#   secrets (gitleaks, when installed; CI always runs it).
set -e
cd "$(dirname "$0")/.."
uv run --quiet ruff check .
uv run --quiet ruff format --check .
uv run --quiet mypy src
uv run --quiet shellcheck scripts/*.sh scripts/git-hooks/*
uv run --quiet codespell src tests docs scripts README.md CHANGELOG.md
if command -v gitleaks >/dev/null 2>&1; then
    gitleaks git --staged --no-banner --redact . >/dev/null
else
    echo "note: gitleaks not installed - secret scan skipped (CI runs it)" >&2
fi
