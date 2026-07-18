"""Quantra MD ingester.

Scheduled worker that writes vendor market data into the ``md.*``
schema (``md.canonical_ids``, ``md.vendor_mappings``,
``md.quote_points``, ``md.snapshots``, ``md.snapshot_quotes``). Always
off the request path. Connection routing uses
:func:`quantra_common.db.make_md_engine` with ``role="rw"`` so the
``md_rw`` pool isolation from the dual-schema split applies.

This package is lifted from
the pre-monorepo market-data service. The lifted modules are
service-local — see ``canonical.py`` / ``connectors/__init__.py`` for
the rationale.
"""

from __future__ import annotations

__version__ = "0.1.0"
