"""Host side of the IRB session tool (irb/DESIGN.md).

- ``protocol``  parses the firmware's lines into typed records
- ``trialinfo`` checks the operator's trial info and sets inflation targets
- ``recorder``  writes one folder per run, plus the session log
- ``link``      the serial link (or a mock) on a reader thread
- ``model``     the live picture of the device the GUI draws from
- ``mock``      a simulated board speaking the same protocol, for tests and demos

Nothing here imports or changes ``host/``; the only shared code is
``host/mapquest``, used read-only by later phases.
"""

PROTOCOL_FIRMWARE = "mapquest-irb-fw-0.2.1"
