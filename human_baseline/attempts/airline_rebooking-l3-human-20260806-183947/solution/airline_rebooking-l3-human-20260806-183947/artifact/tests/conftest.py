"""Put the artifact package root on sys.path so tests can import the agent's modules directly."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
