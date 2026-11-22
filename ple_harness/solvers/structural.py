"""Structural calculation helpers: deflection limits and diaphragm shear.

Assumptions are explicit and narrow. This is NOT an AISC 360 design engine:
full member checks need section properties, limit states, and phi/Omega
factors that come from the engineer and the code tables; only the
self-contained relations below are implemented, each with its documented
applicability.
"""

from __future__ import annotations

from dataclasses import dataclass

from .units import Quantity, SolverInputError, require_finite


def deflection_limit(span_length, span_unit: str, ratio_denominator) -> Quantity:
  """Allowable deflection for a span under an L/n limit (e.g. L/360).

  Args:
    span_length: Span length, in `span_unit`.
    span_unit: A length unit (in, ft, mm, m, ...).
    ratio_denominator: n of L/n — commonly 360 (live load) or 240
      (total load) for typical building members.

  Returns:
    The allowable deflection as a Quantity in `span_unit`.
  """
  length = require_finite(span_length, "span_length", positive=True)
  n = require_finite(ratio_denominator, "ratio_denominator", positive=True)
  if n < 1:
    raise SolverInputError("ratio_denominator must be >= 1 (limit is L/n)")
  return Quantity(length / n, span_unit)


def deflection_check(actual_deflection, span_length, length_unit: str,
                     ratio_denominator) -> dict:
  """Checks a computed deflection against its L/n allowance.

  Returns a dict with the limit, the actual/limit margin, and `passed`.
  Both deflections must be stated in the same `length_unit`.
  """
  actual = require_finite(actual_deflection, "actual_deflection", non_negative=True)
  limit = deflection_limit(span_length, length_unit, ratio_denominator)
  return {
      "allowable": limit.value,
      "unit": length_unit,
      "ratio": f"L/{int(ratio_denominator)}",
      "passed": actual <= limit.value,
      "utilization": actual / limit.value,
  }


@dataclass
class SegmentShear:
  """Shear flow assigned to one wall line / diaphragm segment."""

  segment_index: int
  length: float
  length_unit: str
  shear_per_length: float
  force_per_length_unit: str
  line_shear: float
  force_unit: str


def diaphragm_shear_distribution(total_shear, force_unit: str,
                                 segment_lengths, length_unit: str) -> list[SegmentShear]:
  """Distributes a diaphragm/story shear to segments proportionally to length.

  Assumption (explicit): tributary-width distribution — each segment takes
  V * L_i / sum(L). This ignores torsion, accidental eccentricity,
  rigidity/back-and-forth (rigid vs flexible diaphragm) effects, and
  orthogonal-direction interactions; those come from the engineer's model,
  not from this function. For a flexible diaphragm spanning between stiff
  walls, tributary-width distribution is the standard hand method.

  Args:
    total_shear: The total horizontal shear to distribute (> 0).
    force_unit: Unit of `total_shear` (lbf, kip, N, kN).
    segment_lengths: Non-zero lengths of the resisting segments.
    length_unit: Unit of the segment lengths.

  Returns:
    Per-segment shear flows, in `force_unit` per `length_unit`, plus each
    line's total shear.
  """
  shear = require_finite(total_shear, "total_shear", positive=True)
  lengths = [require_finite(l, f"segment_lengths[{i}]", positive=True)
             for i, l in enumerate(segment_lengths)]
  if not lengths:
    raise SolverInputError("at least one segment length is required")
  total_length = sum(lengths)
  per_length_unit = f"{force_unit}/{length_unit}"
  out = []
  for index, length in enumerate(lengths):
    flow = shear * length / total_length / length  # force per unit length
    out.append(SegmentShear(
        segment_index=index, length=length, length_unit=length_unit,
        shear_per_length=flow, force_per_length_unit=per_length_unit,
        line_shear=flow * length, force_unit=force_unit))
  return out


def demand_capacity_ratio(demand, capacity) -> float:
  """Unity-check helper: demand / capacity, both in the same units.

  Raises on non-finite values, capacity <= 0, or negative demand. A ratio
  <= 1 means the check passes ONLY under the assumptions the caller encoded
  into demand and capacity — this function itself is not a code check.
  """
  d = require_finite(demand, "demand", non_negative=True)
  c = require_finite(capacity, "capacity", positive=True)
  return d / c
