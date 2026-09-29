"""The "brain" of WiseGlow Desk: Claude through the Agent SDK.

- :mod:`sessions` keeps one :class:`ChatState` per Feishu chat (session id,
  lock, queue, buffered group context) and de-duplicates message ids.
- :mod:`tools` exposes the in-process MCP tools (``mcp__wiseglow__*``).
- :mod:`prompt` builds the Italian system prompt.
- :mod:`brain` runs one conversational turn with ``claude_agent_sdk.query``.
"""

from .brain import ClaudeBrain, TurnResult
from .prompt import build_system_prompt
from .sessions import ChatState, SessionRegistry
from .tools import ToolContext, build_tools

__all__ = [
    "ChatState",
    "ClaudeBrain",
    "SessionRegistry",
    "ToolContext",
    "TurnResult",
    "build_system_prompt",
    "build_tools",
]
