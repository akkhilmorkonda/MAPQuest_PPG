"""Laptop side of the expo wireless link (expo/README.md).

    ws          minimal WebSocket client and framing, stdlib only
    linkstats   rates, missing samples and round trip of the stream
    hub         Python model of the link simulator's wireless rules (over
                irbtool.mock.MockBoard), served by sim_server.py

Reuses ``irb/host/irbtool`` read-only (line parser, mock board), the way
``irb/`` reuses ``host/mapquest``.
"""

import sys
from pathlib import Path

_IRB_HOST = Path(__file__).resolve().parents[3] / "irb" / "host"
if str(_IRB_HOST) not in sys.path:
    sys.path.insert(0, str(_IRB_HOST))
