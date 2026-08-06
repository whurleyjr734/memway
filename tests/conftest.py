"""Pytest configuration: ensure coordsys package is importable.

Inserts repo root into sys.path so all test modules can import memway
without requiring PYTHONPATH or an editable install. Loaded automatically
by pytest before any test collection.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
