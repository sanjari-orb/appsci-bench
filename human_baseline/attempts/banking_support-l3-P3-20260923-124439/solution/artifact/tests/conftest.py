"""Make the artifact modules importable from the tests directory."""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_ARTIFACT_DIR = os.path.dirname(_TESTS_DIR)

for _p in (_ARTIFACT_DIR, _TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
