#!/usr/bin/env bash
# Shared runtime selection for Jarvis shell entrypoints.
#
# The setup-managed virtualenv lives outside Desktop so launchd is not blocked
# by macOS TCC. Operators can pin another interpreter with
# JARVIS_PYTHON=/absolute/path/to/python3.

jarvis_select_python() {
  local root="${JARVIS_DIR:-}"
  local candidate="${JARVIS_PYTHON:-}"

  if [ -z "$root" ]; then
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
  fi
  JARVIS_VENV_DIR="${JARVIS_VENV_DIR:-$HOME/.jarvis/runtime-venv}"
  if [ -z "$candidate" ] && [ -x "$JARVIS_VENV_DIR/bin/python3" ]; then
    candidate="$JARVIS_VENV_DIR/bin/python3"
  fi
  if [ -z "$candidate" ]; then
    candidate="$(command -v python3 2>/dev/null || true)"
  fi
  if [ -z "$candidate" ] || [ ! -x "$candidate" ]; then
    echo "ERROR: no usable Python 3 interpreter found." >&2
    echo "       Install Python 3.10+ or set JARVIS_PYTHON=/absolute/path/to/python3." >&2
    return 1
  fi

  candidate="$("$candidate" -c 'import os, sys; print(os.path.abspath(sys.executable))' 2>/dev/null)" \
    || return 1
  JARVIS_PYTHON="$candidate"
  JARVIS_PYTHON_DIR="$(cd "$(dirname "$candidate")" && pwd -P)"
  case ":$PATH:" in
    *":$JARVIS_PYTHON_DIR:"*) ;;
    *) PATH="$JARVIS_PYTHON_DIR:$PATH" ;;
  esac
  export JARVIS_PYTHON JARVIS_PYTHON_DIR JARVIS_VENV_DIR PATH
}

jarvis_select_python
