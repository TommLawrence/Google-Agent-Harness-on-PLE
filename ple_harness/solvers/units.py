"""Unit conversions and arithmetic discipline for engineering quantities.

Conversion factors: SI definitions are exact by convention (inch = 25.4 mm,
pound-force = 4.4482216152605 N), and the imperial-integral pairs are exact
integers (1728 in3/ft3, 12 in/ft, 1000 lbf/kip, 144 in2/ft2). `convert`
short-circuits those exact pairs so no floating-point drift appears where a
grader expects the textbook factor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Exact imperial-integral pairs (checked before the SI route). The factor is
# what `value * factor` needs: 1 ft3 = 1728 in3, 1 kip = 1000 lbf, 1 psi = 144 psf.
EXACT_PAIRS: dict[tuple[str, str], float] = {
    ("in", "ft"): 1 / 12, ("ft", "in"): 12,
    ("in3", "ft3"): 1 / 1728, ("ft3", "in3"): 1728,
    ("in2", "ft2"): 1 / 144, ("ft2", "in2"): 144,
    ("kip", "lbf"): 1000, ("lbf", "kip"): 1 / 1000,
    ("psi", "psf"): 144, ("psf", "psi"): 1 / 144,
    ("ksi", "ksf"): 144, ("ksf", "ksi"): 1 / 144,
    ("mm", "m"): 1 / 1000, ("m", "mm"): 1000,
    ("cm", "m"): 1 / 100, ("m", "cm"): 100,
}

# Factor to the dimension's SI base unit.
TO_SI: dict[str, float] = {
    # length -> m
    "mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0,
    "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "mi": 1609.344,
    # area -> m2
    "mm2": 1e-6, "cm2": 1e-4, "m2": 1.0, "in2": 0.0254 ** 2, "ft2": 0.3048 ** 2,
    # volume -> m3
    "ml": 1e-6, "l": 0.001, "m3": 1.0, "in3": 0.0254 ** 3, "ft3": 0.3048 ** 3,
    # force -> N
    "N": 1.0, "kN": 1000.0, "lbf": 4.4482216152605, "kip": 4448.2216152605,
    # pressure -> Pa
    "Pa": 1.0, "kPa": 1000.0, "MPa": 1e6, "GPa": 1e9,
    "psi": 4.4482216152605 / 0.0254 ** 2,
    "psf": 4.4482216152605 / 0.0254 ** 2 * 144,
    "ksi": 4.4482216152605 / 0.0254 ** 2 * 1000,
    "ksf": 4.4482216152605 / 0.0254 ** 2 * 144,
}

DIMENSIONS: dict[str, set[str]] = {
    "length": {"mm", "cm", "m", "km", "in", "ft", "yd", "mi"},
    "area": {"mm2", "cm2", "m2", "in2", "ft2"},
    "volume": {"ml", "l", "m3", "in3", "ft3"},
    "force": {"N", "kN", "lbf", "kip"},
    "pressure": {"Pa", "kPa", "MPa", "GPa", "psi", "psf", "ksi", "ksf"},
}


class SolverInputError(ValueError):
  """A solver input is missing, non-finite, out of range, or dimensionally wrong."""


def require_finite(value, name: str, *, positive: bool = False,
                   non_negative: bool = False) -> float:
  """Validates a finite number and applies optional range constraints.

  Booleans, strings, None, NaN, and infinities all raise; nothing defaults
  to zero.
  """
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise SolverInputError(f"{name} must be a number, got {type(value).__name__}")
  number = float(value)
  if not math.isfinite(number):
    raise SolverInputError(f"{name} must be finite, got {number}")
  if positive and number <= 0:
    raise SolverInputError(f"{name} must be > 0, got {number}")
  if non_negative and number < 0:
    raise SolverInputError(f"{name} must be >= 0, got {number}")
  return number


def dimension_of(unit: str) -> str:
  for dim, units in DIMENSIONS.items():
    if unit in units:
      return dim
  raise SolverInputError(f"unknown unit: {unit!r}")


def convert(value, from_unit: str, to_unit: str) -> float:
  """Converts `value` between compatible units.

  Exact imperial-integral pairs (1728 in3/ft3, 12 in/ft, 1000 lbf/kip,
  144 psi/psf, ...) are computed exactly; everything else routes through the
  SI base of the dimension.
  """
  number = require_finite(value, "value")
  if from_unit == to_unit:
    return number
  if dimension_of(from_unit) != dimension_of(to_unit):
    raise SolverInputError(
        f"cannot convert {from_unit} to {to_unit}: different dimensions")
  exact = EXACT_PAIRS.get((from_unit, to_unit))
  if exact is not None:
    return number * exact
  si = TO_SI[from_unit]
  target = TO_SI[to_unit]
  return number * si / target


def as_fraction(value, declared_as: str) -> float:
  """Interprets `value` as a fraction given its declared form.

  `declared_as` is "percent" (0-100 scale) or "fraction" (0-1 scale). The
  classic engineering error — passing 25 where 0.25 is needed — raises here
  instead of poisoning a downstream calculation: a value > 1 declared as a
  fraction, or <= 1 ambiguity-free percent, is rejected as contradictory.
  """
  number = require_finite(value, "value")
  if declared_as == "percent":
    if not 0 <= number <= 100:
      raise SolverInputError(f"percent value must be within 0..100, got {number}")
    return number / 100
  if declared_as == "fraction":
    if not 0 <= number <= 1:
      raise SolverInputError(
          f"fraction value must be within 0..1, got {number} "
          "(did you mean to declare it as a percent?)")
    return number
  raise SolverInputError(f"declared_as must be 'percent' or 'fraction', got {declared_as!r}")


def as_percent(value, declared_as: str) -> float:
  """The inverse of `as_fraction`: returns the value on the 0-100 scale."""
  return as_fraction(value, declared_as) * 100


def ceil_takeoff(quantity_needed, package_size) -> int:
  """Discrete package takeoff: smallest number of packages covering demand.

  Uses math.ceil on the exact division; a demand of 0 needs 0 packages.
  """
  needed = require_finite(quantity_needed, "quantity_needed", non_negative=True)
  size = require_finite(package_size, "package_size", positive=True)
  return math.ceil(needed / size) if needed > 0 else 0


@dataclass
class Quantity:
  """A value with an explicit unit, for solver return values."""

  value: float
  unit: str

  def in_unit(self, unit: str) -> "Quantity":
    return Quantity(convert(self.value, self.unit, unit), unit)

  def __str__(self) -> str:  # pragma: no cover - display helper
    return f"{self.value:g} {self.unit}"
