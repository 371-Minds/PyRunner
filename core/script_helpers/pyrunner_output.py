"""
PyRunner Agent I/O helpers.

Provides functions for AI agent scripts to:
  - read structured inputs passed by the caller
  - write structured outputs that agents can read back via the REST API

Usage in scripts::

    from pyrunner_output import get_input, set_output, get_all_inputs

    # Read inputs passed by the agent when triggering this script
    query = get_input("query", default="")
    limit = get_input("limit", default=10)

    # ... do work ...

    # Write structured output
    set_output("result", {"items": [...]})
    set_output("count", 42)
    set_output("success", True)

Notes:
  - ``set_output`` accumulates output in memory and flushes to a temp JSON file
    when the script exits (via ``atexit``).  The executor reads that file and
    stores the data in ``Run.structured_output``.
  - ``get_input`` reads from the ``PYRUNNER_INPUT`` environment variable, which
    the executor sets from the ``inputs`` dict in the API trigger request body.
  - Both functions fail silently when run outside PyRunner so that the same
    script can be tested locally without errors.
"""

import atexit
import json
import os
import sys
from typing import Any

# Accumulated structured output
_output_data: dict = {}
_output_registered = False


def _flush_output() -> None:
    """Write accumulated output to the path specified by PYRUNNER_OUTPUT_PATH."""
    path = os.environ.get("PYRUNNER_OUTPUT_PATH")
    if not path or not _output_data:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_output_data, f)
    except Exception as e:
        # Best-effort — don't crash the script on output failure
        print(f"[pyrunner_output] Warning: could not write output file: {e}", file=sys.stderr)


def _ensure_atexit() -> None:
    """Register flush function exactly once."""
    global _output_registered
    if not _output_registered:
        atexit.register(_flush_output)
        _output_registered = True


def set_output(key: str, value: Any) -> None:
    """
    Set a structured output value.

    Args:
        key:   The output key (string).
        value: Any JSON-serialisable value (dict, list, str, int, float, bool, None).

    The value is accumulated in memory and written to the output file on exit.
    If ``PYRUNNER_OUTPUT_PATH`` is not set (e.g. running locally), this is a no-op.
    """
    _ensure_atexit()
    _output_data[key] = value


def get_input(key: str, default: Any = None) -> Any:
    """
    Read a named input value.

    Inputs are passed by the caller in the ``inputs`` dict of the API trigger
    request body and are serialised into the ``PYRUNNER_INPUT`` environment
    variable as JSON.

    Args:
        key:     The input key to retrieve.
        default: Value to return if the key is not present.

    Returns:
        The input value or ``default``.
    """
    raw = os.environ.get("PYRUNNER_INPUT")
    if not raw:
        return default
    try:
        inputs = json.loads(raw)
        return inputs.get(key, default)
    except (json.JSONDecodeError, AttributeError):
        return default


def get_all_inputs() -> dict:
    """
    Return all inputs as a dictionary.

    Returns an empty dict when running outside PyRunner or when no inputs
    were provided.
    """
    raw = os.environ.get("PYRUNNER_INPUT")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, AttributeError):
        return {}


def get_context(key: str, default: Any = None) -> Any:
    """
    Read a named context value.

    Context values are passed by the caller in the ``context`` dict of the
    API trigger request body and serialised into ``PYRUNNER_CONTEXT``.

    Args:
        key:     The context key to retrieve.
        default: Value to return if the key is not present.
    """
    raw = os.environ.get("PYRUNNER_CONTEXT")
    if not raw:
        return default
    try:
        ctx = json.loads(raw)
        return ctx.get(key, default)
    except (json.JSONDecodeError, AttributeError):
        return default
