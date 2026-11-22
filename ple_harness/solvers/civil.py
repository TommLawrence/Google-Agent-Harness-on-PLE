"""Civil/geotechnical calculation helpers.

Implements the classic Terzaghi one-dimensional consolidation settlement
with an explicit overconsolidated/normally-consolidated regime split, the
2:1 approximate footing stress-spread method, prismoidal earthwork volumes,
and cut/fill balancing. Each relation documents its assumptions; nothing
here claims code compliance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .units import Quantity, SolverInputError, require_finite


def consolidation_settlement(layer_thickness, thickness_unit: str,
                             initial_void_ratio: float,
                             compression_index: float,
                             recompression_index: float,
                             initial_effective_stress, preconsolidation_pressure,
                             final_effective_stress,
                             stress_unit: str) -> tuple[Quantity, str]:
  """One-dimensional consolidation settlement (Terzaghi).

  S = H/(1+e0) * [ Cs*log10(min(σ'f,σ'c)/σ'0)
                   + Cc*log10(σ'f/σ'c) if σ'f > σ'c else 0 ]

  Assumptions (all explicit in the inputs):
  - 1D compression (no lateral strain), saturated fine-grained soil;
  - log base 10 with the standard Terzaghi form;
  - the layer is overconsolidated at rest: σ'0 <= σ'c is required (σ'0 >
    σ'c is a contradiction — the preconsolidation pressure cannot be below
    the current effective stress);
  - the load must increase stress: σ'f >= σ'0 (unloading/rebound is out of
    scope);
  - Cs is the recompression/swell index used between σ'0 and σ'c, Cc the
    virgin compression index beyond σ'c.

  Args:
    layer_thickness: Total layer thickness H (> 0), in `thickness_unit`.
    initial_void_ratio: e0 (> 0).
    compression_index: Cc (>= 0).
    recompression_index: Cs (>= 0).
    initial_effective_stress: σ'0 (> 0), in `stress_unit`.
    preconsolidation_pressure: σ'c (> 0), same unit.
    final_effective_stress: σ'f = σ'0 + Δσ', same unit.

  Returns:
    A `(settlement, regime)` tuple: the settlement as a Quantity in
    `thickness_unit` and the regime string
    ("recompression_only" or "recompression_then_virgin") so callers can
    record which branch governed.
  """
  height = require_finite(layer_thickness, "layer_thickness", positive=True)
  e0 = require_finite(initial_void_ratio, "initial_void_ratio", positive=True)
  cc = require_finite(compression_index, "compression_index", non_negative=True)
  cs = require_finite(recompression_index, "recompression_index", non_negative=True)
  sigma0 = require_finite(initial_effective_stress, "initial_effective_stress", positive=True)
  sigmac = require_finite(preconsolidation_pressure, "preconsolidation_pressure", positive=True)
  sigmaf = require_finite(final_effective_stress, "final_effective_stress", positive=True)

  if sigma0 > sigmac:
    raise SolverInputError(
        f"initial effective stress ({sigma0}) exceeds the preconsolidation "
        f"pressure ({sigmac}): the layer would already be normally consolidated "
        "and σ'c is inconsistent")
  if sigmaf < sigma0:
    raise SolverInputError(
        f"final stress ({sigmaf}) is below the initial stress ({sigma0}): "
        "unloading/rebound is not supported")

  denominator = 1.0 + e0
  if sigmaf <= sigmac:
    # stays on the recompression curve (boundary case included here)
    settlement = cs / denominator * height * math.log10(sigmaf / sigma0)
    regime = "recompression_only"
  else:
    settlement = (cs / denominator * height * math.log10(sigmac / sigma0)
                  + cc / denominator * height * math.log10(sigmaf / sigmac))
    regime = "recompression_then_virgin"
  return Quantity(settlement, thickness_unit), regime  # type: ignore[return-value]


def stress_increase_2to1(footing_load, load_unit: str,
                         footing_width, footing_length,
                         depth_to_plane, length_unit: str) -> Quantity:
  """Vertical stress increase at depth by the 2:1 (2V:1H) spread method.

  Δσ(z) = Q / ((B + z) * (L + z))

  Assumption (explicit): the footing load spreads on a plane rising 2
  vertical to 1 horizontal from the footing edges. This is the standard
  approximate hand method for preliminary settlement checks below the
  footing base; it is not an elastic (Boussinesq) solution and is least
  accurate near the load. All lengths share `length_unit`.
  """
  load = require_finite(footing_load, "footing_load", positive=True)
  width = require_finite(footing_width, "footing_width", positive=True)
  length = require_finite(footing_length, "footing_length", positive=True)
  depth = require_finite(depth_to_plane, "depth_to_plane", non_negative=True)
  area = (width + depth) * (length + depth)
  return Quantity(load / area, f"{load_unit}/{length_unit}^2")


def prismoidal_volume(length, area_a, area_b, area_mid, volume_unit_hint: str = "m3") -> Quantity:
  """Earthwork volume by the prismoidal (Simpson) rule.

  V = L * (A1 + 4*Am + A2) / 6

  Assumption: the end areas and the middle area bound a prism with a
  smooth area variation (the classic prismoidal formula for cut/fill
  stations). Compare against the average-end-area volume when the ground
  is irregular; the difference (prismoidal excess) is a measure of fit.
  Areas are non-negative and share the unit implied by `volume_unit_hint`
  (e.g. m2 for m3, ft2 for ft3, yd2 for yd3).
  """
  import re
  length_m = require_finite(length, "length", positive=True)
  a1 = require_finite(area_a, "area_a", non_negative=True)
  a2 = require_finite(area_b, "area_b", non_negative=True)
  am = require_finite(area_mid, "area_mid", non_negative=True)
  match = re.fullmatch(r"(m3|ft3|yd3|in3|cm3)", volume_unit_hint)
  if not match:
    raise SolverInputError(f"volume_unit_hint must be a cubic length unit, got {volume_unit_hint!r}")
  return Quantity(length_m * (a1 + 4.0 * am + a2) / 6.0, volume_unit_hint)


def cut_fill_balance(cut_volumes, fill_volumes, unit: str = "m3") -> dict:
  """Net earthwork balance from lists of cut and fill volumes.

  Returns the summed cut, fill, and the net (cut - fill): a positive net is
  a surplus to borrow... rather, a surplus to be wasted off-site; a
  negative net needs borrow. Volumes must be non-negative and finite.
  """
  cuts = [require_finite(v, f"cut_volumes[{i}]", non_negative=True)
          for i, v in enumerate(cut_volumes)]
  fills = [require_finite(v, f"fill_volumes[{i}]", non_negative=True)
           for i, v in enumerate(fill_volumes)]
  total_cut, total_fill = sum(cuts), sum(fills)
  return {
      "cut": total_cut,
      "fill": total_fill,
      "net": total_cut - total_fill,
      "unit": unit,
      "status": "surplus" if total_cut > total_fill else
                "deficit" if total_fill > total_cut else "balanced",
  }
