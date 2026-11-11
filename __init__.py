"""Root Hermes Plugin entrypoint for drop-in ~/.hermes/plugins/ discovery."""

import os
import sys

_PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from hermes_cobrowse import register

__all__ = ["register"]
