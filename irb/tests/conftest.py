import sys
from pathlib import Path

IRB = Path(__file__).resolve().parent.parent
REPO = IRB.parent
sys.path.insert(0, str(IRB / "host"))
sys.path.insert(0, str(REPO / "host"))      # host/mapquest, read-only, for compatibility checks
