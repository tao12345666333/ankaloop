"""Agent Client Protocol (ACP) server for AnkaLoop.

Run it over stdio so an ACP client such as Zed or T3 Code can launch
AnkaLoop as its coding agent::

    anka acp serve

The adapter is a thin translation layer over the embedded client: see
:mod:`ankaloop.acp.server`.
"""

from __future__ import annotations

from .server import ACPSessionAgent, run_acp_agent

__all__ = ["ACPSessionAgent", "run_acp_agent"]
