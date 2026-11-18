import sys
from pathlib import Path

EXPO = Path(__file__).resolve().parent.parent
REPO = EXPO.parent
sys.path.insert(0, str(EXPO / "host"))
sys.path.insert(0, str(EXPO / "tools"))
sys.path.insert(0, str(REPO / "irb" / "host"))      # irbtool, read-only (parser, mock board)
