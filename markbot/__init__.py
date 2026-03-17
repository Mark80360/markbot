"""
markbot - A lightweight AI agent framework
"""

import logging
import os

# Suppress LiteLLM logs globally (must be set before importing litellm)
os.environ["LITELLM_LOG"] = "ERROR"
logging.getLogger("LiteLLM").setLevel(logging.ERROR)
logging.getLogger("litellm").setLevel(logging.ERROR)

__version__ = "1.8.9"
__logo__ = "🦞"
