"""Phase 5 Part B: NetBox-vs-Suzieq drift harness.

Read intent from NetBox + state from the Suzieq parquet store, emit
structured drift report. Catches the class of bugs that neither Phase 2
smoke nor Phase 4 Batfish can see: drift between what NetBox says the
network is and what the network actually is, in real time.

Module boundaries:
  intent.py  - the ONLY module that imports pynetbox
  state.py   - the ONLY module that imports pyarrow
  diff.py    - imports neither; pure structured comparison
  cli.py     - the only module that does I/O orchestration

This split keeps the unit tests dependency-light: test_drift_diff.py
and test_drift_cli.py import nothing heavier than pandas, and use
hand-built dicts as fixtures for both intent and state.
"""
