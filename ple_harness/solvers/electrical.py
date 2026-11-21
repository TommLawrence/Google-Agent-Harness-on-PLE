"""Electrical calculation helpers: voltage drop and takeoff arithmetic.

Voltage drop is computed from explicit conductor resistance (or resistivity)
— never from a hardcoded NEC table. The caller supplies table-derived values
(resistance per length, derating factors); this module does the arithmetic
and refuses to guess. Nothing here claims NEC compliance: it performs the
math an engineer checks against the code tables.

The cumulative path model is explicit: the caller passes the ordered
sequence of segment drops from source to load, and the total is the sum —
an intermediate panel never resets the percentage.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .units import Quantity, SolverInputError, TO_SI, DIMENSIONS, require_finite

# Standard annealed conductor resistivity at 20 °C (Ω·mm²/m) — the IACS
# reference values, provided so callers can derive resistance when the
# instruction gives conductor size and material but no table value.
RHO_CU_20C = 0.017241  # ohm*mm2/m
RHO_AL_20C = 0.028214  # ohm*mm2/m


@dataclass
class VoltageDrop:
  """One segment's drop, with its percent of the source voltage."""

  volts: float
  percent: float
  source_voltage: float


def voltage_drop(current_a, one_way_length, length_unit: str,
                 resistance_per_length, resistance_unit: str,
                 source_voltage, phases: int) -> VoltageDrop:
  """Voltage drop for one run of conductor.

  VD_1ph = 2 * I * r * L   (out and return conductor)
  VD_3ph = sqrt(3) * I * r * L   (line-to-line, balanced three-phase)

  Args:
    current_a: Load current (A, > 0).
    one_way_length: One-way circuit length, in `length_unit`.
    length_unit: Length unit of `one_way_length` (ft, m, ...).
    resistance_per_length: Conductor resistance per unit length, in
      `resistance_unit` (e.g. 'ohm/1000ft' or 'ohm/km') — from the
      conductor table the engineer selects, at the operating temperature.
    resistance_unit: Compound unit "ohm/<length>".
    source_voltage: System voltage the percent is taken against (> 0).
    phases: 1 or 3.

  Returns:
    A `VoltageDrop` with volts and percent of `source_voltage`.
  """
  current = require_finite(current_a, "current_a", positive=True)
  length = require_finite(one_way_length, "one_way_length", positive=True)
  resistance = require_finite(resistance_per_length, "resistance_per_length", positive=True)
  voltage = require_finite(source_voltage, "source_voltage", positive=True)
  if phases not in (1, 3):
    raise SolverInputError(f"phases must be 1 or 3, got {phases}")

  r_per_length_unit = _resistance_per(resistance, resistance_unit, length_unit)
  two_way = 2.0 if phases == 1 else math.sqrt(3.0)
  drop_volts = two_way * current * r_per_length_unit * length
  return VoltageDrop(drop_volts, drop_volts / voltage * 100.0, voltage)


def _resistance_per(resistance: float, resistance_unit: str,
                    length_unit: str) -> float:
  """Normalizes a table resistance ('ohm/ft', 'ohm/km', 'ohm/1000ft', ...) to ohms per `length_unit`."""
  factor = resistance_unit.split("/", 1)[-1].strip()
  multiplier, base = 1.0, factor
  if factor.startswith("k") and factor[1:] in DIMENSIONS["length"]:
    multiplier, base = 1000.0, factor[1:]
  else:
    scaled = re.fullmatch(r"1000(ft|m|yd|mi)", factor)
    if scaled:
      multiplier, base = 1000.0, scaled.group(1)
  if base not in DIMENSIONS["length"]:
    raise SolverInputError(
        "resistance_unit must look like 'ohm/<length unit>' "
        "(e.g. ohm/ft, ohm/km, ohm/1000ft), got " + repr(resistance_unit))
  # ohms per base unit, then per length_unit
  return (resistance / multiplier) * (TO_SI[length_unit] / TO_SI[base])


def resistance_from_resistivity(resistivity_ohm_mm2_m, area_mm2,
                                one_way_length, length_unit: str,
                                temperature_correction=1.0) -> Quantity:
  """Conductor resistance from resistivity, cross-section, and length.

  R = rho * L / A, with an explicit temperature correction factor applied
  to rho (1.0 = 20 °C reference). Pass the correction from the table the
  engineer selects; nothing is assumed here.
  """
  rho = require_finite(resistivity_ohm_mm2_m, "resistivity", positive=True)
  area = require_finite(area_mm2, "area_mm2", positive=True)
  length = require_finite(one_way_length, "one_way_length", positive=True)
  correction = require_finite(temperature_correction, "temperature_correction", positive=True)
  from .units import TO_SI
  length_m = length * TO_SI[length_unit]
  ohms = rho * correction * length_m / area
  return Quantity(ohms, "ohm")


@dataclass
class CumulativeDrop:
  """Cumulative source-to-load drop over an explicit ordered path."""

  total_volts: float
  percent: float
  source_voltage: float
  segments: list[VoltageDrop]
  within_limit: bool | None  # None when no limit was supplied


def cumulative_voltage_drop(segment_drops: list[VoltageDrop],
                            max_percent=None) -> CumulativeDrop:
  """Sums an ordered source-to-load path of segment drops.

  The total is the arithmetic sum of the segment voltages; the percent is
  that sum against the source voltage. Adding percents directly is wrong
  when segments share the same source and this function prevents it by
  taking `VoltageDrop` objects only. An intermediate panel between segments
  does not reset anything: the caller models the whole path source→load in
  order. When `max_percent` is given it must come from the instruction's
  stated limit; the comparison is total <= limit.
  """
  if not segment_drops:
    raise SolverInputError("at least one segment drop is required")
  source = segment_drops[0].source_voltage
  total = 0.0
  for index, segment in enumerate(segment_drops):
    require_finite(segment.volts, f"segment_drops[{index}].volts", non_negative=True)
    if segment.source_voltage != source:
      raise SolverInputError(
          f"segment {index} cites source {segment.source_voltage} V, "
          f"segment 0 cites {source} V: model one path against one source")
    total += segment.volts
  percent = total / source * 100.0
  limit = None
  if max_percent is not None:
    limit = require_finite(max_percent, "max_percent", positive=True)
  return CumulativeDrop(total, percent, source, list(segment_drops),
                        None if limit is None else percent <= limit)


def derated_ampacity(base_ampacity_a, temperature_correction,
                     conductor_adjustment) -> float:
  """Applies explicit derating factors to a table ampacity.

  I' = I * Ct * Cadj

  Both factors are REQUIRED arguments (pass 1.0 explicitly when a factor
  does not apply) — silently skipping a factor is exactly the failure mode
  this function exists to prevent. The base ampacity comes from the
  applicable table the caller selected; this function is arithmetic, not a
  code determination.
  """
  base = require_finite(base_ampacity_a, "base_ampacity_a", positive=True)
  ct = require_finite(temperature_correction, "temperature_correction", positive=True)
  cadj = require_finite(conductor_adjustment, "conductor_adjustment", positive=True)
  return base * ct * cadj


def min_ampacity_for_load(load_a, continuous_fraction, multiplier=1.25) -> float:
  """Minimum conductor/overcurrent sizing for a load with a continuous part.

  I_min = (continuous part) * multiplier + (non-continuous part)

  `continuous_fraction` is declared as a fraction (0..1) via as_fraction by
  the caller or passed here as a percent using `continuous_percent`.
  `multiplier` defaults to the standard 125% continuous factor but must be
  stated explicitly by the caller when the instruction uses another value.
  """
  load = require_finite(load_a, "load_a", positive=True)
  fraction = require_finite(continuous_fraction, "continuous_fraction", non_negative=True)
  if fraction > 1:
    raise SolverInputError(
        f"continuous_fraction must be a fraction (0..1), got {fraction} "
        "(declare percent values with as_fraction first)")
  factor = require_finite(multiplier, "multiplier", positive=True)
  return load * fraction * factor + load * (1.0 - fraction)


def takeoff_packages(needed, package_size) -> int:
  """Discrete device/carton takeoff (ceil), shared with units.ceil_takeoff."""
  from .units import ceil_takeoff
  return ceil_takeoff(needed, package_size)
