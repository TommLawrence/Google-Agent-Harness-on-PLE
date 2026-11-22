"""Offline tests for the ple_harness solver library (Phase C).

Every solver has at least one independently hand-checked numerical example,
its boundary cases (notably the preconsolidation regime split), and
invalid-input cases (non-finite, wrong sign, wrong dimension, missing
discipline like percent-vs-fraction).
"""

from __future__ import annotations

import math
import unittest

from ple_harness.solvers import (civil, electrical, mechanical, structural)
from ple_harness.solvers import units
from ple_harness.solvers.units import SolverInputError


class UnitsTests(unittest.TestCase):

  def test_exact_imperial_volume_factor(self):
    # the assignment's headline case: 1728 in3 per ft3, exactly
    self.assertEqual(units.convert(1728, "in3", "ft3"), 1.0)
    self.assertEqual(units.convert(1, "ft3", "in3"), 1728)
    self.assertEqual(units.convert(3456, "in3", "ft3"), 2.0)

  def test_force_length_pressure_conversions(self):
    self.assertEqual(units.convert(5, "kip", "lbf"), 5000)
    self.assertEqual(units.convert(2500, "mm", "m"), 2.5)
    self.assertEqual(units.convert(144, "psf", "psi"), 1.0)
    self.assertAlmostEqual(units.convert(3, "ft", "m"), 0.9144, places=12)
    self.assertAlmostEqual(units.convert(100, "kPa", "psi"), 14.50377377, places=6)

  def test_incompatible_dimensions_raise(self):
    with self.assertRaises(SolverInputError):
      units.convert(5, "kip", "ft")
    with self.assertRaises(SolverInputError):
      units.convert(5, "psi", "m3")

  def test_require_finite_rejects_every_non_number(self):
    for bad in (float("nan"), float("inf"), -float("inf"), "5", True, None, [1]):
      with self.assertRaises(SolverInputError):
        units.require_finite(bad, "x")
    with self.assertRaises(SolverInputError):
      units.require_finite(-1, "x", positive=True)
    with self.assertRaises(SolverInputError):
      units.require_finite(0, "x", positive=True)

  def test_percent_fraction_discipline(self):
    self.assertEqual(units.as_fraction(25, "percent"), 0.25)
    self.assertEqual(units.as_fraction(0.25, "fraction"), 0.25)
    self.assertEqual(units.as_percent(0.25, "fraction"), 25.0)
    with self.assertRaises(SolverInputError):  # the classic mistake
      units.as_fraction(25, "fraction")
    with self.assertRaises(SolverInputError):
      units.as_fraction(101, "percent")
    with self.assertRaises(SolverInputError):
      units.as_fraction(5, "banana")

  def test_ceil_takeoff(self):
    self.assertEqual(units.ceil_takeoff(10, 6), 2)
    self.assertEqual(units.ceil_takeoff(12, 6), 2)
    self.assertEqual(units.ceil_takeoff(13.5, 4), 4)
    self.assertEqual(units.ceil_takeoff(0, 6), 0)
    with self.assertRaises(SolverInputError):
      units.ceil_takeoff(10, 0)
    with self.assertRaises(SolverInputError):
      units.ceil_takeoff(-1, 6)


class StructuralTests(unittest.TestCase):

  def test_deflection_limit_hand_checked(self):
    # 20 ft span at L/360: 20/360 ft = 0.0556 ft = 0.6667 in
    limit = structural.deflection_limit(20, "ft", 360)
    self.assertAlmostEqual(limit.value, 20 / 360, places=12)
    self.assertAlmostEqual(limit.in_unit("in").value, 0.6666667, places=6)
    self.assertEqual(structural.deflection_limit(20, "ft", 240).value, 20 / 240)

  def test_deflection_check(self):
    failed = structural.deflection_check(0.5, 20, "ft", 360)
    self.assertFalse(failed["passed"])
    self.assertAlmostEqual(failed["utilization"], 0.5 / (20 / 360), places=9)
    self.assertTrue(structural.deflection_check(0.05, 20, "ft", 360)["passed"])
    with self.assertRaises(SolverInputError):
      structural.deflection_limit(20, "ft", 0.5)  # L/0.5 is nonsense

  def test_diaphragm_shear_distribution_hand_checked(self):
    # 10000 lbf over 20 ft + 30 ft: proportional-to-length gives uniform
    # flow of 200 lbf/ft and line shears of 4000/6000
    flows = structural.diaphragm_shear_distribution(10000, "lbf", [20, 30], "ft")
    self.assertAlmostEqual(flows[0].shear_per_length, 200.0, places=9)
    self.assertAlmostEqual(flows[1].shear_per_length, 200.0, places=9)
    self.assertAlmostEqual(flows[0].line_shear, 4000.0, places=9)
    self.assertAlmostEqual(flows[1].line_shear, 6000.0, places=9)
    self.assertAlmostEqual(sum(f.line_shear for f in flows), 10000.0, places=9)
    flows = structural.diaphragm_shear_distribution(10000, "lbf", [10, 30], "ft")
    self.assertAlmostEqual(flows[0].shear_per_length, 250.0, places=9)
    self.assertAlmostEqual(flows[1].shear_per_length, 250.0, places=9)

  def test_diaphragm_shear_rejects_bad_segments(self):
    with self.assertRaises(SolverInputError):
      structural.diaphragm_shear_distribution(1000, "lbf", [], "ft")
    with self.assertRaises(SolverInputError):
      structural.diaphragm_shear_distribution(1000, "lbf", [10, 0], "ft")
    with self.assertRaises(SolverInputError):
      structural.diaphragm_shear_distribution(0, "lbf", [10], "ft")

  def test_utilization_helper(self):
    self.assertAlmostEqual(structural.demand_capacity_ratio(45, 60), 0.75)
    with self.assertRaises(SolverInputError):
      structural.demand_capacity_ratio(45, 0)


class CivilConsolidationTests(unittest.TestCase):

  def test_normally_consolidated_hand_checked(self):
    # H=4 m, e0=0.9, Cc=0.36, sigma'0 = sigma'c = 100 kPa, sigma'f = 200:
    # S = 4/1.9 * 0.36 * log10(2) = 0.228149 m
    settlement, regime = civil.consolidation_settlement(
        4, "m", 0.9, 0.36, 0.06, 100, 100, 200, "kPa")
    self.assertAlmostEqual(settlement.value, 0.2281490, places=6)
    self.assertEqual(regime, "recompression_then_virgin")

  def test_overconsolidated_stays_on_recompression_curve(self):
    # sigma'f below sigma'c: only Cs acts
    # S = 0.06/1.9 * 4 * log10(120/50) = 0.048027 m
    settlement, regime = civil.consolidation_settlement(
        4, "m", 0.9, 0.36, 0.06, 50, 150, 120, "kPa")
    self.assertAlmostEqual(settlement.value, 0.048027, places=5)
    self.assertEqual(regime, "recompression_only")

  def test_crossing_the_preconsolidation_boundary(self):
    # both branches: Cs to sigma'c then Cc beyond
    # S = 0.06/1.9*4*log10(150/50) + 0.36/1.9*4*log10(300/150)
    expected = (0.06 / 1.9 * 4 * math.log10(3)
                + 0.36 / 1.9 * 4 * math.log10(2))
    settlement, regime = civil.consolidation_settlement(
        4, "m", 0.9, 0.36, 0.06, 50, 150, 300, "kPa")
    self.assertAlmostEqual(settlement.value, expected, places=12)
    self.assertEqual(regime, "recompression_then_virgin")

  def test_boundary_sigma_f_equals_sigma_c_matches_both_branches(self):
    at_boundary, _ = civil.consolidation_settlement(
        4, "m", 0.9, 0.36, 0.06, 50, 150, 150, "kPa")
    manual = 0.06 / 1.9 * 4 * math.log10(150 / 50)
    self.assertAlmostEqual(at_boundary.value, manual, places=12)

  def test_invalid_stress_states_raise(self):
    with self.assertRaises(SolverInputError):  # sigma'0 above sigma'c
      civil.consolidation_settlement(4, "m", 0.9, 0.36, 0.06, 200, 150, 300, "kPa")
    with self.assertRaises(SolverInputError):  # unloading
      civil.consolidation_settlement(4, "m", 0.9, 0.36, 0.06, 100, 150, 90, "kPa")
    for bad in (float("nan"), 0, -1):
      with self.assertRaises(SolverInputError):
        civil.consolidation_settlement(4, "m", bad, 0.36, 0.06, 100, 150, 200, "kPa")


class CivilEarthworkTests(unittest.TestCase):

  def test_2to1_stress_spread_hand_checked(self):
    # Q=1000 kN on 2 m x 2 m footing, plane at z=3 m: (5)(5)=25 -> 40 kPa
    stress = civil.stress_increase_2to1(1000, "kN", 2, 2, 3, "m")
    self.assertAlmostEqual(stress.value, 40.0, places=9)
    self.assertEqual(stress.unit, "kN/m^2")

  def test_prismoidal_volume_hand_checked(self):
    # L=30 m, A1=10, Am=14, A2=20 -> 30*(10 + 4*14 + 20)/6 = 430 m3
    volume = civil.prismoidal_volume(30, 10, 20, 14, "m3")
    self.assertAlmostEqual(volume.value, 430.0, places=9)

  def test_prismoidal_rejects_bad_inputs(self):
    with self.assertRaises(SolverInputError):
      civil.prismoidal_volume(30, -1, 20, 14, "m3")
    with self.assertRaises(SolverInputError):
      civil.prismoidal_volume(30, 10, 20, 14, "bananas")

  def test_cut_fill_balance(self):
    result = civil.cut_fill_balance([100, 50], [120], "m3")
    self.assertEqual(result["net"], 30)
    self.assertEqual(result["status"], "surplus")
    self.assertEqual(civil.cut_fill_balance([120], [120.0], "m3")["status"], "balanced")
    self.assertEqual(civil.cut_fill_balance([100], [130], "m3")["status"], "deficit")
    with self.assertRaises(SolverInputError):
      civil.cut_fill_balance([-5], [1], "m3")


class ElectricalTests(unittest.TestCase):

  def test_single_phase_drop_hand_checked(self):
    # I=20 A, L=100 ft, r=0.5 ohm/1000ft, 1ph: VD = 2*20*0.0005*100 = 2.0 V
    drop = electrical.voltage_drop(20, 100, "ft", 0.5, "ohm/1000ft", 120, 1)
    self.assertAlmostEqual(drop.volts, 2.0, places=9)
    self.assertAlmostEqual(drop.percent, 2.0 / 120 * 100, places=9)

  def test_three_phase_drop_uses_sqrt3(self):
    # I=20 A, L=100 ft, r=0.5 ohm/1000ft: VD = sqrt(3)*20*0.05 = 1.73205 V
    drop = electrical.voltage_drop(20, 100, "ft", 0.5, "ohm/1000ft", 208, 3)
    self.assertAlmostEqual(drop.volts, math.sqrt(3) * 20 * 0.05, places=9)

  def test_metric_resistance_units(self):
    # r=1.84 ohm/km, L=500 m, I=10 A, 1ph: VD = 2*10*0.00184*500 = 18.4 V
    drop = electrical.voltage_drop(10, 500, "m", 1.84, "ohm/km", 480, 1)
    self.assertAlmostEqual(drop.volts, 18.4, places=9)

  def test_resistance_unit_validation(self):
    with self.assertRaises(SolverInputError):
      electrical.voltage_drop(10, 100, "ft", 0.5, "ohms-ish", 120, 1)
    with self.assertRaises(SolverInputError):
      electrical.voltage_drop(10, 100, "ft", 0.5, "ohm/ft", 120, 2)

  def test_resistance_from_resistivity_hand_checked(self):
    # #10 AWG copper (5.26 mm2), 100 m: R = 0.017241*100/5.26 = 0.32778 ohm
    r = electrical.resistance_from_resistivity(electrical.RHO_CU_20C, 5.26, 100, "m")
    self.assertAlmostEqual(r.value, 0.017241 * 100 / 5.26, places=12)
    r60 = electrical.resistance_from_resistivity(electrical.RHO_CU_20C, 5.26, 100, "m",
                                                 temperature_correction=1.2)
    self.assertAlmostEqual(r60.value, r.value * 1.2, places=12)

  def test_cumulative_drop_does_not_reset_at_panels(self):
    # feeder: 3ph, I=100 A, 200 ft, r=0.1 ohm/1000ft -> sqrt(3)*2.0 = 3.464 V
    # branch: 1ph, I=20 A, 150 ft, r=0.5 ohm/1000ft -> 2*1.5 = 3.0 V
    feeder = electrical.voltage_drop(100, 200, "ft", 0.1, "ohm/1000ft", 480, 3)
    branch = electrical.voltage_drop(20, 150, "ft", 0.5, "ohm/1000ft", 480, 1)
    self.assertAlmostEqual(feeder.volts, math.sqrt(3) * 2.0, places=9)
    self.assertAlmostEqual(branch.volts, 3.0, places=9)
    total = electrical.cumulative_voltage_drop([feeder, branch], max_percent=1.0)
    self.assertAlmostEqual(total.total_volts, feeder.volts + branch.volts, places=9)
    self.assertAlmostEqual(total.percent,
                           (feeder.volts + branch.volts) / 480 * 100, places=9)
    self.assertFalse(total.within_limit)  # 1.35% exceeds the 1% limit
    ok = electrical.cumulative_voltage_drop([feeder, branch], max_percent=5)
    self.assertTrue(ok.within_limit)

  def test_cumulative_rejects_mixed_sources_and_empty(self):
    a = electrical.VoltageDrop(2.0, 1.7, 120)
    b = electrical.VoltageDrop(1.0, 0.4, 240)
    with self.assertRaises(SolverInputError):
      electrical.cumulative_voltage_drop([a, b])
    with self.assertRaises(SolverInputError):
      electrical.cumulative_voltage_drop([])

  def test_derating_requires_explicit_factors(self):
    self.assertAlmostEqual(electrical.derated_ampacity(30, 0.88, 0.8), 21.12, places=9)
    self.assertAlmostEqual(electrical.derated_ampacity(30, 1.0, 0.8), 24.0, places=9)

  def test_min_ampacity_for_continuous_load(self):
    self.assertAlmostEqual(electrical.min_ampacity_for_load(20, 1.0), 25.0, places=9)
    self.assertAlmostEqual(electrical.min_ampacity_for_load(20, 0.0), 20.0, places=9)
    self.assertAlmostEqual(electrical.min_ampacity_for_load(20, 0.5), 22.5, places=9)
    with self.assertRaises(SolverInputError):  # percent passed where fraction needed
      electrical.min_ampacity_for_load(20, 50)

  def test_takeoff(self):
    self.assertEqual(electrical.takeoff_packages(55, 25), 3)


class MechanicalTests(unittest.TestCase):

  def test_darcy_weisbach_hand_checked(self):
    # f=0.02, L=100 m, D=0.1 m, V=2 m/s: hf = 0.02*1000*(4/19.6133) = 4.0789 m
    head = mechanical.darcy_weisbach_head_loss(0.02, 100, 0.1, 2.0)
    self.assertAlmostEqual(head.value, 4.07886, places=4)
    self.assertEqual(head.unit, "m")
    zero = mechanical.darcy_weisbach_head_loss(0.02, 100, 0.1, 0.0)
    self.assertEqual(zero.value, 0.0)

  def test_darcy_weisbach_rejects_bad_inputs(self):
    for args in [(0, 100, 0.1, 2), (-0.02, 100, 0.1, 2), (0.02, 0, 0.1, 2),
                 (0.02, 100, -0.1, 2), (float("nan"), 100, 0.1, 2)]:
      with self.assertRaises(SolverInputError):
        mechanical.darcy_weisbach_head_loss(*args)

  def test_colebrook_matches_moody_chart_value(self):
    # Re=1e5, eps/D=1e-4: the Moody chart reads f ~ 0.0185
    f = mechanical.friction_factor_colebrook(1e5, 1e-4)
    self.assertAlmostEqual(f, 0.0185, delta=0.0005)
    # fixed point on the equation itself: residual must vanish
    rhs = -2 * math.log10(1e-4 / 3.7 + 2.51 / (1e5 * math.sqrt(f)))
    self.assertAlmostEqual(1 / math.sqrt(f), rhs, places=8)

  def test_colebrook_rejects_laminar_and_extremes(self):
    with self.assertRaises(SolverInputError):
      mechanical.friction_factor_colebrook(2000, 0.001)
    with self.assertRaises(SolverInputError):
      mechanical.friction_factor_colebrook(1e5, 0.5)

  def test_lifecycle_npv_hand_checked(self):
    # C0=50k, A=5k/yr, r=5%, 25 yr: PW = 50k + 5k*14.09394 = 120469.72
    result = mechanical.lifecycle_npv(50000, 5000, 0.05, 25)
    self.assertAlmostEqual(result.present_worth, 120469.72, delta=0.5)
    result = mechanical.lifecycle_npv(50000, 5000, 0.05, 25,
                                      replacement_cost=20000, replacement_year=10)
    self.assertAlmostEqual(result.present_worth, 120469.72 + 20000 / 1.05 ** 10,
                           delta=0.5)
    result = mechanical.lifecycle_npv(50000, 5000, 0.05, 25, salvage_value=3000)
    self.assertAlmostEqual(result.present_worth, 120469.72 - 3000 / 1.05 ** 25,
                           delta=0.5)

  def test_zero_discount_rate_is_a_plain_sum(self):
    result = mechanical.lifecycle_npv(50000, 5000, 0.0, 25)
    self.assertAlmostEqual(result.present_worth, 175000.0, places=9)

  def test_npv_rejects_bad_rates_and_partial_replacements(self):
    with self.assertRaises(SolverInputError):
      mechanical.lifecycle_npv(50000, 5000, 1.0, 25)   # 100% rate
    with self.assertRaises(SolverInputError):
      mechanical.lifecycle_npv(50000, 5000, 50, 25)    # percent, not fraction
    with self.assertRaises(SolverInputError):
      mechanical.lifecycle_npv(50000, 5000, 0.05, 25.5)  # fractional years
    with self.assertRaises(SolverInputError):
      mechanical.lifecycle_npv(50000, 5000, 0.05, 25, replacement_cost=10)
    with self.assertRaises(SolverInputError):
      mechanical.lifecycle_npv(50000, 5000, 0.05, 25, replacement_year=10)

  def test_aabb_intersection_boundary_cases(self):
    a = ((0, 0, 0), (1, 1, 1))
    # touching corner counts as intersecting
    self.assertTrue(mechanical.aabb_intersect(*a, (1, 1, 1), (2, 2, 2)))
    # clearly separated
    self.assertFalse(mechanical.aabb_intersect(*a, (1.1, 0, 0), (2, 1, 1)))
    # contained
    self.assertTrue(mechanical.aabb_intersect(*a, (0.25, 0.25, 0.25), (0.5, 0.5, 0.5)))

  def test_aabb_overlap_volume(self):
    volume = mechanical.aabb_overlap_volume((0, 0, 0), (2, 2, 2), (1, 1, 1), (3, 3, 3))
    self.assertAlmostEqual(volume.value, 1.0, places=12)
    self.assertEqual(mechanical.aabb_overlap_volume(
        (0, 0, 0), (1, 1, 1), (2, 2, 2), (3, 3, 3)).value, 0.0)

  def test_aabb_validates_boxes(self):
    with self.assertRaises(SolverInputError):
      mechanical.aabb_intersect((1, 1, 1), (0, 0, 0), (0, 0, 0), (1, 1, 1))
    with self.assertRaises(SolverInputError):
      mechanical.aabb_intersect((0, 0), (1, 1), (0, 0), (1, 1))
    with self.assertRaises(SolverInputError):
      mechanical.aabb_intersect((0, 0, float("nan")), (1, 1, 1), (0, 0, 0), (1, 1, 1))


if __name__ == "__main__":
  unittest.main()
