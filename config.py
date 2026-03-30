"""
Application feature flags.

Environment variables:
    XLF_MULTILANG=1    enable multi-language translation UI (default: hidden)
"""
import os

MULTI_LANG_ENABLED: bool = os.getenv("XLF_MULTILANG", "0") == "1"
