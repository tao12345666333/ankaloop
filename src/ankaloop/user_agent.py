"""Global User-Agent for AnkaLoop's outbound provider traffic.

Two callers send it:

- LLM provider SDK clients (any-llm -> OpenAI/Anthropic SDKs), via
  ``default_headers``
- models.dev database fetches (``models_db.fetch_models_from_api``)

Both identify AnkaLoop in the endpoint's request logs instead of showing up as
the bare library. ``ANKA_EXTRA_UA`` appends an additional deployment-specific
token for gateways that require per-deployment identification.

Precedence for the ``User-Agent`` value: an explicit ``extra_headers`` entry
wins over the AnkaLoop default. Because the OpenAI/Anthropic SDKs merge
client-level ``default_headers`` last, the AnkaLoop default *replaces* the
SDK's built-in UA (e.g. ``OpenAI/Python ...``) rather than being appended to
it; per-request headers set by callers on top of ``default_headers`` still win.
"""

from __future__ import annotations

import os

from ._version import __version__ as _VERSION


def ankaloop_user_agent() -> str:
    """Return the global UA string, e.g. ``AnkaLoop/0.18.6``.

    ``ANKA_EXTRA_UA`` (when set) is appended verbatim after a space, so
    operators can add deployment markers like ``AnkaLoop/0.18.6 prod-east``.
    """
    extra = os.environ.get("ANKA_EXTRA_UA", "").strip()
    base = f"AnkaLoop/{_VERSION}"
    return f"{base} {extra}" if extra else base


def default_headers(user_headers: dict[str, str] | None = None) -> dict[str, str]:
    """Return client default headers carrying the global UA.

    ``user_headers`` win over the global UA, so callers can override the exact
    ``User-Agent`` through ``extra_headers``.
    """
    return {"User-Agent": ankaloop_user_agent(), **(user_headers or {})}
