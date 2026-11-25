"""Locate the pinned OpenGP5 source and its Python modules."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "opengp5"
PLATFORM = SDK / "platform"
HOST = ROOT / "platform" / "host"

if not (PLATFORM / "manifest.py").is_file():
    raise SystemExit("Initialize OpenGP5 with git submodule update --init")

sys.path[1:1] = [str(SDK / "tools"), str(PLATFORM)]
