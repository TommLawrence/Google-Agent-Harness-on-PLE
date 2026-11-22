"""Offline tests for ple_harness.authority (document authority resolution)."""

from __future__ import annotations

import unittest

from ple_harness import authority
from ple_harness.authority import Document, Supersedes, resolve_authority


def doc(doc_id: str, doc_type: str, **kw) -> Document:
  return Document(id=doc_id, path=f"/files/{doc_id}", doc_type=doc_type, **kw)


class RevisionAndDateParsingTests(unittest.TestCase):

  def test_revision_keys(self):
    self.assertEqual(authority.revision_key("Rev 2"), (2,))
    self.assertEqual(authority.revision_key("R2"), (2,))
    self.assertEqual(authority.revision_key("Revision 10"), (10,))
    self.assertGreater(authority.revision_key("rev 10"), authority.revision_key("rev 9"))
    self.assertEqual(authority.revision_key("Revision 3A"), (3, 1))
    self.assertEqual(authority.revision_key("B"), (2,))
    self.assertGreater(authority.revision_key("2B"), authority.revision_key("2A"))
    self.assertIsNone(authority.revision_key(None))
    self.assertIsNone(authority.revision_key("unparseable!"))

  def test_parse_date(self):
    self.assertEqual(authority.parse_date("Issued 2026-07-04"), "2026-07-04")
    self.assertIsNone(authority.parse_date("sometime in July"))
    self.assertIsNone(authority.parse_date(None))

  def test_document_validation(self):
    with self.assertRaises(ValueError):
      doc("x", "newsletter")
    with self.assertRaises(ValueError):
      doc("x", "ifc", issue_status="kind-of-issued")


class ResolutionTests(unittest.TestCase):

  def test_ifc_outranks_preliminary_globally(self):
    result = resolve_authority([
        doc("prelim", "preliminary", revision="1"),
        doc("ifc", "ifc", revision="B", issue_status="issued_for_construction"),
    ])
    self.assertEqual(result.governing_for("global"), "ifc")
    self.assertFalse(result.unresolved_scopes())

  def test_superseded_preliminary_is_excluded(self):
    result = resolve_authority(
        [doc("prelim", "preliminary", revision="1"),
         doc("ifc", "ifc", revision="B")],
        supersedes=[Supersedes(source="ifc", target="prelim",
                               note="IFC set supersedes preliminary")])
    self.assertEqual(result.governing_for("global"), "ifc")
    self.assertTrue(any("superseded by ifc" in t for t in result.trace))

  def test_scoped_addendum_governs_only_its_scope(self):
    # an addendum that changes only the footing detail must not invalidate
    # the whole preliminary package, but it does govern the footing scope
    prelim = doc("prelim", "preliminary", revision="1", title="Prelim structural set")
    addendum = doc("addn2", "addendum", revision="2",
                   scope_tags=["footing"], title="Addendum 2: footing revision")
    result = resolve_authority([prelim, addendum])
    self.assertEqual(result.governing_for("footing"), "addn2")
    self.assertEqual(result.governing_for("global"), "prelim")

  def test_rfi_answer_governs_its_scope_over_ifc(self):
    result = resolve_authority([
        doc("ifc", "ifc", revision="B"),
        doc("rfi9", "rfi_response", scope_tags=["roof_drain"]),
    ])
    self.assertEqual(result.governing_for("roof_drain"), "rfi9")
    self.assertEqual(result.governing_for("global"), "ifc")

  def test_ahj_letter_outranks_everything_by_default(self):
    result = resolve_authority([
        doc("rfi9", "rfi_response", scope_tags=["roof_drain"]),
        doc("ifc", "ifc", revision="B"),
        doc("ahj", "ahj_letter", title="AHJ determination"),
    ])
    self.assertEqual(result.governing_for("global"), "ahj")

  def test_conflicting_revisions_same_type(self):
    result = resolve_authority([
        doc("ifc_r2", "ifc", revision="Rev 2"),
        doc("ifc_r3", "ifc", revision="Rev 3"),
    ])
    self.assertEqual(result.governing_for("global"), "ifc_r3")
    self.assertTrue(any("Rev 3" in r for r in result.decisions["global"].reasons))

  def test_missing_date_falls_back_to_revision_then_flags_confidence(self):
    result = resolve_authority([
        doc("a", "ifc", revision="Rev 1"),           # no date anywhere
        doc("b", "ifc", revision="Rev 2"),
    ])
    self.assertEqual(result.governing_for("global"), "b")
    self.assertEqual(result.decisions["global"].confidence, "medium")

  def test_unresolvable_pair_is_a_conflict_not_a_silent_win(self):
    result = resolve_authority([
        doc("x", "other"),
        doc("y", "other"),
    ])
    decision = result.decisions["global"]
    self.assertIsNone(decision.governing)
    self.assertIn("cannot discriminate", " ".join(decision.conflicts))
    self.assertIn("global", result.unresolved_scopes())

  def test_status_tie_break(self):
    result = resolve_authority([
        doc("draft", "other", issue_status="draft"),
        doc("appr", "other", issue_status="approved"),
    ])
    self.assertEqual(result.governing_for("global"), "appr")

  def test_datum_conflict_is_flagged(self):
    result = resolve_authority([
        doc("navd", "ifc", revision="1", datum="NAVD88"),
        doc("ngvd", "permit_bid", revision="1", datum="NGVD29"),
    ])
    decision = result.decisions["global"]
    self.assertEqual(decision.governing, "navd")  # IFC outranks permit/bid
    self.assertTrue(any("datums" in c for c in decision.conflicts))
    self.assertEqual(decision.confidence, "low")
    self.assertIn("global", result.unresolved_scopes())

  def test_sole_candidate_is_high_confidence(self):
    result = resolve_authority([doc("only", "ifc")])
    self.assertEqual(result.decisions["global"].confidence, "high")

  def test_marked_superseded_status_is_excluded_even_without_claim(self):
    result = resolve_authority([
        doc("old", "ifc", issue_status="superseded"),
        doc("new", "ifc", revision="2"),
    ])
    self.assertEqual(result.governing_for("global"), "new")

  def test_result_dict_round_trip(self):
    result = resolve_authority([doc("ifc", "ifc", revision="1")])
    as_dict = result.to_dict()
    self.assertIn("global", as_dict["decisions"])
    self.assertTrue(as_dict["trace"])


if __name__ == "__main__":
  unittest.main()
