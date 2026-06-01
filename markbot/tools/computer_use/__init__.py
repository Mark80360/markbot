"""Computer use toolset — cross-platform desktop control for markbot.

Architecture
------------
* ``backend.py``          — abstract ``ComputerUseBackend``; swappable implementation.
* ``cua_backend.py``      — macOS backend; speaks MCP over stdio to ``cua-driver``.
* ``pyautogui_backend.py`` — cross-platform backend (Linux/Windows/macOS) via pyautogui.
* ``noop_backend.py``     — test/CI stub. Records calls; returns trivial results.
* ``tool.py``             — registers the ``computer_use`` tool via the tool system.

Backend selection (automatic by default):
  - macOS + cua-driver installed  → ``cua`` backend (background, no cursor steal)
  - Linux/Windows + pyautogui     → ``pyautogui`` backend (foreground, real cursor)
  - Override via ``MARKBOT_COMPUTER_USE_BACKEND`` env var or config ``backend`` key.
"""

from markbot.tools.computer_use.tool import ComputerUseTool

__all__ = ["ComputerUseTool"]
