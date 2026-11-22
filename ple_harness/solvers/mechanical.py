"""Mechanical calculation helpers: Darcy–Weisbach, lifecycle NPV, AABB.

The 3D axis-aligned bounding-box tests are a broad-phase clash detector for
BIM/IFC workflows: two objects whose AABBs do not intersect cannot collide;
intersecting AABBs do NOT prove an exact geometric collision. Narrow-phase
checks stay with the model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .units import Quantity, SolverInputError, require_finite

STANDARD_GRAVITY = 9.80665  # m/s2, ISO standard value


def darcy_weisbach_head_loss(friction_factor, length_m, diameter_m,
                             velocity_m_per_s, gravity=STANDARD_GRAVITY) -> Quantity:
  """Friction head loss in a circular pipe: hf = f * (L/D) * V^2 / (2g).

  Assumptions: steady, fully developed, incompressible flow in a circular
  pipe; Darcy (not Fanning) friction factor; velocity is the bulk average.
  The friction factor itself (Moody/Colebrook/fitted) is an explicit input —
  this module does not compute it from Reynolds number unless asked via
  `friction_factor_colebrook`.

  Args:
    friction_factor: Darcy f (> 0, dimensionless).
    length_m: Pipe length (m, > 0).
    diameter_m: Inner diameter (m, > 0).
    velocity_m_per_s: Mean velocity (m/s, >= 0).
    gravity: g in m/s2 (> 0; the ISO standard default is explicit).

  Returns:
    Head loss as a Quantity in meters of water column.
  """
  f = require_finite(friction_factor, "friction_factor", positive=True)
  length = require_finite(length_m, "length_m", positive=True)
  diameter = require_finite(diameter_m, "diameter_m", positive=True)
  velocity = require_finite(velocity_m_per_s, "velocity_m_per_s", non_negative=True)
  g = require_finite(gravity, "gravity", positive=True)
  head = f * (length / diameter) * velocity ** 2 / (2.0 * g)
  return Quantity(head, "m")


def friction_factor_colebrook(reynolds, relative_roughness,
                              max_iterations: int = 100,
                              tolerance: float = 1e-10) -> float:
  """Solves the Colebrook–White equation for the Darcy friction factor.

  1/sqrt(f) = -2*log10( eps/(3.7*D) + 2.51/(Re*sqrt(f)) )

  Solved by fixed-point iteration f <- (1/rhs)^2 from the Swamee–Jain
  starting value — the standard, provably convergent arrangement. Valid for
  turbulent flow (Re > ~4000); laminar flow uses f = 64/Re — this function
  raises for Re below the turbulent range rather than silently applying the
  wrong branch. `relative_roughness` is eps/D (0..0.05).
  """
  re = require_finite(reynolds, "reynolds", positive=True)
  rel = require_finite(relative_roughness, "relative_roughness", non_negative=True)
  if rel > 0.05:
    raise SolverInputError(f"relative_roughness should be <= 0.05, got {rel}")
  if re < 4000:
    raise SolverInputError(
        f"reynolds {re} is below the turbulent range (~4000); use f = 64/Re "
        "for laminar flow instead")
  if re > 1e12:
    raise SolverInputError(f"reynolds {re} is beyond the equation's validated range")

  f = 0.25 / (math.log10(rel / 3.7 + 5.74 / re ** 0.9) ** 2)  # Swamee–Jain
  for _ in range(max_iterations):
    rhs = -2.0 * math.log10(rel / 3.7 + 2.51 / (re * math.sqrt(f)))
    f_next = (1.0 / rhs) ** 2
    if abs(f_next - f) < tolerance * f:
      return f_next
    f = f_next
  return f


@dataclass
class NPVResult:
  present_worth: float
  cash_currency_unit: str
  years: int
  discount_rate: float
  breakdown: dict


def lifecycle_npv(capital_cost, annual_cost, discount_rate, years,
                  currency_unit: str = "USD",
                  replacement_cost=None, replacement_year=None,
                  salvage_value=None) -> NPVResult:
  """Present worth of a lifecycle cost stream (e.g. a 25-year LCC).

  PW = C0 + sum_{t=1..N} A / (1+r)^t
       + R / (1+r)^t_rep   (single replacement, optional)
       - S / (1+r)^N       (salvage credited, optional)

  All rates are fractions (use as_fraction for percents). Costs are
  non-negative in `currency_unit`; `discount_rate` may be 0 (plain sum).
  The discounting convention (end-of-year cash flows) is explicit and
  uniform; mid-year conventions are out of scope.
  """
  from .units import as_fraction  # callers convert percent rates themselves
  capital = require_finite(capital_cost, "capital_cost", non_negative=True)
  annual = require_finite(annual_cost, "annual_cost", non_negative=True)
  rate = require_finite(discount_rate, "discount_rate", non_negative=True)
  if not 0 <= rate < 1:
    raise SolverInputError(
        f"discount_rate must be a fraction within [0, 1) (convert percent "
        "rates with as_fraction first), got " + repr(rate))
  n = require_finite(years, "years", positive=True)
  if n != int(n):
    raise SolverInputError(f"years must be a whole number, got {years}")
  n = int(n)

  total = capital
  breakdown = {"capital": capital, "annual_stream": 0.0}
  for year in range(1, n + 1):
    total += annual / (1.0 + rate) ** year
  breakdown["annual_stream"] = total - capital

  if replacement_cost is not None or replacement_year is not None:
    if replacement_cost is None or replacement_year is None:
      raise SolverInputError("replacement_cost and replacement_year go together")
    cost = require_finite(replacement_cost, "replacement_cost", non_negative=True)
    when = require_finite(replacement_year, "replacement_year", positive=True)
    if when != int(when) or not 1 <= when <= n:
      raise SolverInputError(
          f"replacement_year must be a whole year within 1..{n}, got {when}")
    total += cost / (1.0 + rate) ** int(when)
    breakdown["replacement"] = cost / (1.0 + rate) ** int(when)

  if salvage_value is not None:
    salvage = require_finite(salvage_value, "salvage_value", non_negative=True)
    credit = salvage / (1.0 + rate) ** n
    total -= credit
    breakdown["salvage_credit"] = credit

  return NPVResult(total, currency_unit, n, rate, breakdown)


def aabb_intersect(a_min, a_max, b_min, b_max) -> bool:
  """True when two 3D axis-aligned bounding boxes intersect (touching counts).

  Boxes are (x, y, z) min/max corner tuples in any consistent length unit.
  Broad-phase only: non-intersection proves separation; intersection does
  NOT prove the underlying geometry collides.
  """
  a_lo = _validated_box(a_min, "a_min")
  a_hi = _validated_box(a_max, "a_max")
  b_lo = _validated_box(b_min, "b_min")
  b_hi = _validated_box(b_max, "b_max")
  for axis in range(3):
    if a_lo[axis] > a_hi[axis] or b_lo[axis] > b_hi[axis]:
      raise SolverInputError("box min corner exceeds its max corner")
    if a_lo[axis] > b_hi[axis] or b_lo[axis] > a_hi[axis]:
      return False
  return True


def aabb_overlap_volume(a_min, a_max, b_min, b_max) -> Quantity:
  """Volume of the AABB intersection (0 when separated); same unit caveat.

  The volume unit is the caller's length unit cubed — pass the unit so the
  result is explicit.
  """
  a_lo, a_hi = _validated_box(a_min, "a_min"), _validated_box(a_max, "a_max")
  b_lo, b_hi = _validated_box(b_min, "b_min"), _validated_box(b_max, "b_max")
  extents = []
  for axis in range(3):
    if a_lo[axis] > a_hi[axis] or b_lo[axis] > b_hi[axis]:
      raise SolverInputError("box min corner exceeds its max corner")
    overlap = min(a_hi[axis], b_hi[axis]) - max(a_lo[axis], b_lo[axis])
    if overlap <= 0:
      return Quantity(0.0, "unit3")
    extents.append(overlap)
  return Quantity(extents[0] * extents[1] * extents[2], "unit3")


def _validated_box(corner, name) -> tuple[float, float, float]:
  if len(corner) != 3:
    raise SolverInputError(f"{name} must have three coordinates")
  values = tuple(require_finite(v, f"{name}[{i}]") for i, v in enumerate(corner))
  return values
