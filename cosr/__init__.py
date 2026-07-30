"""Co-SR measurement control plane.

Modules that also run ON the testbed nodes (`radiotap`, `wire`, `agent`, `natsc`, `nl80211`)
are constrained to Python 3.4 and the standard library -- the AP image ships Python 3.4.0 with
no pip. `tests/test_py34_compat.py` enforces that; controller-only modules are unrestricted.
"""
