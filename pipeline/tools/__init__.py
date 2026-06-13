"""Tool registry + dispatch.

Architecture defense pillar 2: each tool returns predictable data; errors
come back as data the agent can react to, never crashes. Pillar 3 action
guardrail: allow-list, scope permissions.

Both invariants enforced in dispatch.py.

Tools 1/2/3 are built in parallel by other agents. The protocols here are
the contract — when those tools ship, they implement these protocols and
get registered in `tools.registry`.
"""
from .registry import (
    register_tool, get_tool, list_tools, ToolNotAllowed,  # noqa: F401
)
from .dispatch import dispatch  # noqa: F401
from .adapters import (
    PickTimestampsTool, CropVideoTool, AddCaptionsTool,  # noqa: F401
)
