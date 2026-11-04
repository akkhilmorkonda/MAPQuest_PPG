# benchtop_data

Recordings from benchtop experiments, kept apart from the human cuff trials in `data/`.
`marker_report.py` scores every `data/hr_*.csv`, so bench runs must never go there.

## Flow bench (intralipid phantom)

Written by `host/flow_bench.py` (D49). Each recording starts when the board streams and
ends with Stop recording (R) or when the window closes:

- `flow_<date>_<time>[_<run ID>].csv`: the raw IR and red counts. Every marker (`no_flow`,
  `flow`), re-tune and link reset is logged inline at its session time.
- `..._steps.csv`: one row per marker. Each row compares the median before and after the
  marker, and gives the change in counts and %, ΔOD and the SNR, for IR and red. The SNR
  noise is measured on the flowing side of each marker.
- `..._analysis.png`: a hand-made analysis figure, for runs that got one.
- `..._report.png`: written only by `python host/flow_bench.py --report <file>`.
