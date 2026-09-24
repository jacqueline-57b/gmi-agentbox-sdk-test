"""The runtimes this account serves, named once for the two files that need it.

`tests/test_client.py` holds the tripwire: it asks `eligibility()` what the set
is and fails if the service and this tuple ever disagree.
`tests/test_catalog.py` parametrizes every `idcs.list` / `products.list` row
over the same tuple — a collection-time decision, which is why this is a plain
constant and not a fixture.

Keeping both in one place is the point: a runtime the service adds shows up as
a failed tripwire in one file and as new parametrized rows in the other, off a
single edit here.
"""

from __future__ import annotations

# Measured 2026-09-24 against https://ce-tot.gmicloud-dev.com with 0.1.0b2:
# eligibility() reports {"container": {...}, "sandbox": {...}}.
RUNTIMES = ("sandbox", "container")
