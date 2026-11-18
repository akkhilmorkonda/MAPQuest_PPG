"""Laptop side of the expo wireless link (expo/README.md).

    ws          minimal WebSocket client and framing, stdlib only
    linkstats   rates, missing samples and round trip of the stream
    hub         Python model of the board's wireless rules (over
                irbtool.mock.MockBoard), served by sim_server.py
    results     the results manager: records each run, runs host/mapquest,
                grades the result (expo_results.py drives it)

Reuses ``irb/host/irbtool`` (line parser, mock board) and ``host/mapquest``
(the analysis) read-only, the way ``irb/`` reuses ``host/mapquest``.
"""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "irb" / "host", _REPO / "host"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
