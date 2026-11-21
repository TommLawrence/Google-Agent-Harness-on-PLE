"""Deterministic engineering solvers (Phase C).

Rules every function here follows:

- inputs carry explicit units in their names or parameters; nothing accepts
  "a number" with an implied unit;
- no missing input silently becomes zero — absent required data raises
  `SolverInputError`;
- all inputs are validated as finite numbers before any math runs;
- assumptions are documented in each docstring, and a mathematical result is
  never presented as a code-compliance determination (no function here claims
  NEC/AISC/IFC compliance on its own);
- outputs are plain floats or small dataclasses carrying value + unit.

These helpers are for the harness roles to run calculations reproducibly —
they do not look up code tables (NEC ampacity tables, AISC member tables);
where a table value is needed, it is an explicit parameter.
"""

from .units import (  # noqa: F401  re-exported for convenient access
    SolverInputError,
    as_fraction,
    as_percent,
    ceil_takeoff,
    convert,
)
