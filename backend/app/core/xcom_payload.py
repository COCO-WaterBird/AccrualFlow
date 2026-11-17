"""
Defensive normalisation of Airflow XCom payloads.

Why this module exists
----------------------
Airflow can serialise the return value of a TaskFlow task in two different
shapes:

* the Task SDK writer stores the **object** itself::

      {"reporting_month": "2026-11", "legal_entity": "2000"}

* the classic writer stores the **JSON text** of that object::

      "{\"reporting_month\": \"2026-11\", \"legal_entity\": \"2000\"}"

Both writers can be active inside a single DAG run.  Tasks executed by the
scheduler run in a worker process, while tasks executed in-process (for
example ``airflow dags test``) use the classic writer.  Values are always
read back through the execution API, and its deserializer only rebuilds
real JSON objects.  A payload written by the classic writer therefore
arrives as a ``str`` that still contains the JSON document.

Indexing such a payload raises::

    TypeError: string indices must be integers, not 'str'

Every consumer of an XCom payload therefore normalises it first.  The
helpers are pure functions without an Airflow dependency, so they can be
unit tested without a running Airflow instance.
"""

from __future__ import annotations

import json
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _decode(value: Any) -> Any:
    """Return the JSON document behind a JSON-text payload.

    A value that is not JSON text is returned unchanged.  Only payloads that
    start with a JSON structural character are decoded, so that a plain
    string such as ``"2026-11"`` is never mistaken for JSON.
    """
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="replace")

    if not isinstance(value, str):
        return value

    text = value.strip()

    if not text or text[0] not in "[{\"":
        return value

    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def as_dict(value: Any, context: str = "xcom") -> dict:
    """Return the XCom payload as a dictionary.

    Both the object representation and the JSON-text representation are
    accepted.  ``None`` is treated as an empty payload.
    """
    decoded = _decode(value)

    if decoded is None:
        return {}

    if isinstance(decoded, dict):
        return decoded

    raise TypeError(
        f"{context}: expected a mapping in the XCom payload, "
        f"got {type(decoded).__name__}"
    )


def as_list(value: Any, context: str = "xcom") -> list:
    """Return the XCom payload as a list.

    Both the array representation and the JSON-text representation are
    accepted.  ``None`` is treated as an empty payload.
    """
    decoded = _decode(value)

    if decoded is None:
        return []

    if isinstance(decoded, list):
        return decoded

    raise TypeError(
        f"{context}: expected a list in the XCom payload, "
        f"got {type(decoded).__name__}"
    )


def as_str(value: Any, context: str = "xcom") -> str:
    """Return the XCom payload as a string.

    Both the plain string representation and the JSON-text representation
    are accepted.  ``None`` is treated as an empty payload.
    """
    decoded = _decode(value)

    if decoded is None:
        return ""

    if isinstance(decoded, str):
        return decoded

    raise TypeError(
        f"{context}: expected a string in the XCom payload, "
        f"got {type(decoded).__name__}"
    )
