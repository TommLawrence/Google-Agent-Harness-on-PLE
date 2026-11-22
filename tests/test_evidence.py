"""Offline tests for ple_harness.evidence (the evidence ledger)."""

from __future__ import annotations

import unittest

from ple_harness import evidence
from ple_harness.evidence import (COMPUTED, INTERPRETATION, SOURCE, VERIFICATION,
                                  Evidence, EvidenceLedger)


def source_entry(**kw):
  base = dict(id="S1", kind=SOURCE, claim="allowable bearing pressure",
              value="3000", unit="psf", source_path="geotech.pdf",
              source_location="page 4, table 2")
  base.update(kw)
  return Evidence(**base)


class LedgerRuleTests(unittest.TestCase):

  def test_duplicate_ids_rejected(self):
    ledger = EvidenceLedger([source_entry()])
    with self.assertRaises(ValueError):
      ledger.add(source_entry(id="S1"))

  def test_interpretation_cannot_enter_verified_without_verifier(self):
    entry = Evidence(id="M1", kind=INTERPRETATION, claim="value means allowable",
                     verification=evidence.VERIFIED)
    ledger = EvidenceLedger([entry])
    self.assertEqual(ledger.get("M1").verification, evidence.UNVERIFIED)
    self.assertEqual(ledger.get("M1").verifier_ref, "")

  def test_mark_verified_requires_verifier_ref(self):
    ledger = EvidenceLedger([source_entry()])
    with self.assertRaises(ValueError):
      ledger.mark_verified("S1", "")

  def test_mark_verified_and_failed(self):
    ledger = EvidenceLedger([source_entry()])
    ledger.mark_verified("S1", "verifier-run-1", passed=True)
    self.assertEqual(ledger.get("S1").verification, evidence.VERIFIED)
    self.assertEqual(ledger.get("S1").verifier_ref, "verifier-run-1")
    ledger.mark_verified("S1", "verifier-run-2", passed=False)
    self.assertEqual(ledger.get("S1").verification, evidence.FAILED)

  def test_missing_get_raises_key_error(self):
    with self.assertRaises(KeyError):
      EvidenceLedger().get("nope")

  def test_unresolved_lists_interpretation_and_computed_only(self):
    ledger = EvidenceLedger([
        source_entry(),
        Evidence(id="M1", kind=INTERPRETATION, claim="3000 psf is allowable"),
        Evidence(id="C1", kind=COMPUTED, claim=" footing demand", rule="q = P/A"),
        Evidence(id="V1", kind=VERIFICATION, claim="cross-checked"),
    ])
    self.assertEqual([e.id for e in ledger.unresolved()], ["M1", "C1"])
    ledger.mark_verified("M1", "v1")
    self.assertEqual([e.id for e in ledger.unresolved()], ["C1"])

  def test_failed_listing(self):
    ledger = EvidenceLedger([Evidence(id="C1", kind=COMPUTED, claim="x")])
    ledger.mark_verified("C1", "v", passed=False)
    self.assertEqual([e.id for e in ledger.failed()], ["C1"])

  def test_by_kind(self):
    ledger = EvidenceLedger([source_entry(), Evidence(id="M1", kind=INTERPRETATION, claim="y")])
    self.assertEqual(len(ledger.by_kind(SOURCE)), 1)
    self.assertEqual(len(ledger.by_kind(INTERPRETATION)), 1)


class SerializationTests(unittest.TestCase):

  def test_json_round_trip_preserves_everything(self):
    entry = Evidence(
        id="S1", kind=SOURCE, claim="column load", value="180", unit="kip",
        source_path="drawings.pdf", source_location="S2.1, detail 5",
        authority_ref="IFC rev B", conversion="1 kip = 1000 lbf",
        rule="P = sum of reactions", constraints=["LRFD"],
        derived_from=["S0"], note="keep")
    ledger = EvidenceLedger([entry])
    restored = EvidenceLedger.from_json(ledger.to_json())
    self.assertEqual(restored.digest(), ledger.digest())
    self.assertEqual(restored.get("S1").to_dict(), entry.to_dict())

  def test_digest_changes_on_modification(self):
    ledger = EvidenceLedger([source_entry()])
    before = ledger.digest()
    ledger.add(source_entry(id="S2"))
    self.assertNotEqual(before, ledger.digest())

  def test_invalid_json_rejected(self):
    with self.assertRaises(ValueError):
      EvidenceLedger.from_json('{"nope": 1}')
    with self.assertRaises(ValueError):
      EvidenceLedger.from_json('{"entries": [{"id": "X", "kind": "bogus"}]}')
    with self.assertRaises(ValueError):
      EvidenceLedger.from_json('{"entries": [{"kind": "source"}]}')


class RenderTests(unittest.TestCase):

  def test_render_is_stable_and_marks_unresolved(self):
    ledger = EvidenceLedger([
        source_entry(),
        Evidence(id="C1", kind=COMPUTED, claim="settlement", value="0.62",
                 unit="in", rule="S = Cc*H/(1+e0)*log(...)"),
    ])
    one = ledger.render()
    two = ledger.render()
    self.assertEqual(one, two)  # stable across renders
    self.assertIn("S1 | source | source", one)
    self.assertIn("0.62 in", one)
    self.assertIn("[unresolved]", one)
    self.assertIn("geotech.pdf#page 4, table 2", one)

  def test_render_truncation_note(self):
    entries = [source_entry(id=f"S{i}") for i in range(5)]
    rendered = EvidenceLedger(entries).render(max_entries=2)
    self.assertIn("3 more entries omitted", rendered)


if __name__ == "__main__":
  unittest.main()
