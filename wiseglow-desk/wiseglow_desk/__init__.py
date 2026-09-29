"""WiseGlow Desk: a Feishu (Lark) group bot that drafts branded business documents.

Team members talk to the bot in a Feishu group (or DM), send it materials and
ask in natural language for NBO, LOI, land expressions of interest, contracts
and any other document. Claude (through the Claude Agent SDK) does the
thinking; a single deterministic renderer applies the WiseGlow visual
identity, so content is free and presentation is always the same.

Sub-packages:

- :mod:`wiseglow_desk.feishu`  REST client, event parsing, long connection
- :mod:`wiseglow_desk.agent`   Claude sessions, prompt and in-process tools
- :mod:`wiseglow_desk.docgen`  Document model, brand, DOCX/PDF rendering, store
- :mod:`wiseglow_desk.bot`     the ``Desk`` wiring everything together
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
