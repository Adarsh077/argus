"""Phase 7: screenshot sampling + cloud vision narratives.

Everything in this package is called on demand only — from the CLI when a
report is generated with ``--vision``. Nothing here is ever imported or
invoked from the daemon / background capture loop (see capture/screen.py,
daemon.py): screenshots are captured continuously, but they are only ever
sent to a cloud vision API when a human explicitly asks for a report with
``--vision``.
"""

from __future__ import annotations
