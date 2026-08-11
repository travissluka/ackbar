"""The interactive console: `ackbar-ui`, or `ackbar ui`.

Everything under here may import `textual` and `rich`. Nothing outside here may,
and nothing outside here imports this package at module scope. That line is what
keeps the console's dependencies out of every environment a job runs in: a job
imports `ackbar.run`, `ackbar.run` imports nothing from `ackbar.ui`, and so a
compute node with no textual installed is unaffected by the console existing.

`theme.py` and `discover.py` are the exception in the other direction: they
import neither, so the palette and the experiment scan stay testable in a bare
environment.

`ackbar.cli` reaches this package from inside a command function for the same
reason.
"""
