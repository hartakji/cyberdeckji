#!/usr/bin/env python3
"""Cyberdeck entry point.   python3 deck.py [--windowed]"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from deckfw.main import main  # noqa: E402

if __name__ == "__main__":
    main(ROOT)
