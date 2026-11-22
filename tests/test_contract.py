"""Offline tests for ple_harness.contract (deliverable contract + validation)."""

from __future__ import annotations

import io
import json
import unittest
import zipfile

from ple_harness import contract


def oxml_bytes(fmt: str = "docx") -> bytes:
  """A minimal valid OOXML container with the right internal name."""
  buf = io.BytesIO()
  with zipfile.ZipFile(buf, "w") as bundle:
    bundle.writestr("[Content_Types].xml", "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>")
    if fmt == "docx":
      bundle.writestr("word/document.xml", "<w/>")
    else:
      bundle.writestr("xl/workbook.xml", "<w/>")
  return buf.getvalue()


class MemIO(contract.TaskIO):
  def __init__(self, files: dict[str, bytes]):
    self.files = files
    self.reads: list[str] = []

  async def read_bytes(self, path: str) -> bytes:
    self.reads.append(path)
    if path in self.files:
      return self.files[path]
    raise contract.MissingFile(path)

  async def exists(self, path: str) -> bool:
    return path in self.files


INSTRUCTION = """
Standards Engineering needs the witness-test scope package. Read the letters in
/files/letters. Write the scope memo to /app/files/output/Witness_Scope_Memo.docx
and export the register as `witness_register.csv` with columns: Circuit, Length_ft, Amps.
The register must include a row for Panel LP-1 and a row for Panel LP-2.
When you are done, reply with a summary of what you delivered.
"""


class ParseContractTests(unittest.TestCase):

  def test_explicit_write_target_is_confirmed(self):
    c = contract.parse_contract(INSTRUCTION)
    paths = {r.path: r for r in c.files}
    self.assertIn("/app/files/output/Witness_Scope_Memo.docx", paths)
    self.assertEqual(paths["/app/files/output/Witness_Scope_Memo.docx"].confidence,
                     contract.CONFIRMED)

  def test_backticked_deliverable_is_confirmed(self):
    c = contract.parse_contract(INSTRUCTION)
    paths = {r.path: r for r in c.files}
    self.assertIn("witness_register.csv", paths)
    self.assertEqual(paths["witness_register.csv"].confidence, contract.CONFIRMED)

  def test_mentioned_only_paths_are_inferred(self):
    c = contract.parse_contract("Background: see /files/letters/approval.pdf for context.")
    paths = {r.path: r for r in c.files}
    if "/files/letters/approval.pdf" in paths:  # source docs may be picked up
      self.assertEqual(paths["/files/letters/approval.pdf"].confidence,
                       contract.INFERRED)

  def test_csv_columns_land_in_schema(self):
    c = contract.parse_contract(INSTRUCTION)
    self.assertEqual(c.csv_schemas.get("witness_register.csv"),
                     ["Circuit", "Length_ft", "Amps"])

  def test_register_rows_are_captured(self):
    c = contract.parse_contract(INSTRUCTION)
    rows = c.register_rows.get("witness_register.csv", [])
    self.assertIn("Panel LP-1", rows)
    self.assertIn("Panel LP-2", rows)

  def test_final_message_detected(self):
    c = contract.parse_contract(INSTRUCTION)
    self.assertTrue(c.final_message_required)
    self.assertFalse(contract.parse_contract("Compute the beam size.").final_message_required)

  def test_column_list_without_target_is_recorded_as_issue(self):
    text = "The columns: Depth, Width, Span must be reported."
    c = contract.parse_contract(text)
    self.assertTrue(any("column list without a target" in i.detail for i in c.issues))

  def test_model_extraction_merges_confirmed_entries(self):
    def extractor(text):
      return {
          "files": [
              {"path": "/app/out/report.md", "quote": "submit /app/out/report.md",
               "description": "final report"},
              {"path": "/app/out/data.csv",
               "required_columns": ["Node", "Moment_kNm"],
               "required_rows": ["Node N7"]},
          ],
          "final_message_required": True,
      }

    c = contract.parse_contract("plain text", model_extract=extractor)
    paths = {r.path: r for r in c.files}
    self.assertEqual(paths["/app/out/report.md"].confidence, contract.CONFIRMED)
    self.assertEqual(c.csv_schemas["/app/out/data.csv"], ["Node", "Moment_kNm"])
    self.assertEqual(c.register_rows["/app/out/data.csv"], ["Node N7"])
    self.assertTrue(c.final_message_required)

  def test_invalid_model_entries_are_dropped_with_issue(self):
    def extractor(text):
      return {"files": [{"bogus": True}, {"path": "../escape.md"}], "files2": None}

    c = contract.parse_contract("plain text", model_extract=extractor)
    self.assertFalse(c.files)
    self.assertTrue(any("dropped invalid file entry" in i.detail for i in c.issues))

  def test_failing_extractor_becomes_issue_not_crash(self):
    def extractor(text):
      raise RuntimeError("no model here")

    c = contract.parse_contract("text", model_extract=extractor)
    self.assertTrue(any("model extraction failed" in i.detail for i in c.issues))

  def test_outside_workspace_is_flagged(self):
    text = "Write the summary to /etc/passwd_report.md"
    c = contract.parse_contract(text, workspace_root="/app")
    outside = [i for i in c.issues if "escapes the workspace" in i.detail]
    self.assertTrue(outside, "a path outside the workspace must be flagged")


class PathHelperTests(unittest.TestCase):

  def test_resolve_and_containment(self):
    self.assertEqual(contract.resolve_path("out/a.txt", "/app/files"),
                     "/app/files/out/a.txt")
    self.assertEqual(contract.resolve_path("/app/x", "/app/files"), "/app/x")
    self.assertTrue(contract.path_within("out/a.txt", "/app/files"))
    self.assertTrue(contract.path_within("/app/files/out/a.txt", "/app/files"))
    self.assertFalse(contract.path_within("../escape.txt", "/app/files"))
    self.assertFalse(contract.path_within("/etc/passwd", "/app/files"))
    self.assertTrue(contract.path_within("/etc/passwd", "/"))


class FormatSniffTests(unittest.TestCase):

  def test_pdf_signature(self):
    self.assertIsNone(contract.sniff(b"%PDF-1.7 ...", "a.pdf"))
    self.assertIsNotNone(contract.sniff(b"NOT A PDF", "a.pdf"))

  def test_office_container(self):
    self.assertIsNone(contract.sniff(oxml_bytes("docx"), "m.docx"))
    self.assertIsNone(contract.sniff(oxml_bytes("xlsx"), "m.xlsx"))
    self.assertIn("PK", contract.sniff(b"hello", "m.docx"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as bundle:
      bundle.writestr("other.txt", "x")
    self.assertIn("[Content_Types].xml", contract.sniff(buf.getvalue(), "m.docx"))

  def test_ifc_step_header(self):
    self.assertIsNone(contract.sniff(b"ISO-10303-21;\nHEADER;", "m.ifc"))
    self.assertIsNotNone(contract.sniff(b"Something else", "m.ifc"))

  def test_dxf_group_structure(self):
    self.assertIsNone(contract.sniff(b"0\nSECTION\n2\nHEADER\n", "m.dxf"))
    self.assertIsNone(contract.sniff(b"AutoCAD Binary DXF\r\n\x1a\x00", "m.dxf"))
    self.assertIsNotNone(contract.sniff(b"random", "m.dxf"))

  def test_xml_json_python_text(self):
    self.assertIsNone(contract.sniff(b"<a><b/></a>", "a.xml"))
    self.assertIn("invalid XML", contract.sniff(b"<a><b></a>", "a.xml"))
    self.assertIsNone(contract.sniff(b'{"k": 1}', "a.json"))
    self.assertIn("invalid JSON", contract.sniff(b"{oops", "a.json"))
    self.assertIsNone(contract.sniff(b"x = 1\n", "a.py"))
    self.assertIn("invalid Python", contract.sniff(b"def broken(:\n", "a.py"))
    self.assertIsNone(contract.sniff("café".encode(), "a.txt"))
    self.assertIn("UTF-8", contract.sniff(b"\xff\xfe\x00bad", "a.txt"))

  def test_unknown_extension_is_not_checked(self):
    self.assertIsNone(contract.sniff(b"whatever", "a.bin"))


class CsvCheckTests(unittest.TestCase):

  def test_schema_nan_and_rows(self):
    data = (b"Circuit,Length_ft,Amps\nLP-1,120,15\nLP-2,nan,20\n")
    problems = contract.check_csv_bytes(data, ["Circuit", "Length_ft", "Amps"],
                                        ["Panel LP-1"])
    self.assertTrue(any("non-finite" in p for p in problems))
    self.assertTrue(any("missing required row" in p for p in problems))
    ok = contract.check_csv_bytes(b"Circuit,Length_ft,Amps\nPanel LP-1,10,5\n",
                                  ["circuit", "length_ft", "amps"], ["Panel LP-1"])
    self.assertEqual(ok, [])

  def test_missing_column_reported(self):
    problems = contract.check_csv_bytes(b"Ampacity\n15\n", ["Ampacity", "Length_ft"], [])
    self.assertTrue(any("missing required columns: ['Length_ft']" in p for p in problems))

  def test_empty_csv(self):
    self.assertEqual(contract.check_csv_bytes(b"", [], []),
                     ["csv has no data rows"])


class ValidateDeliverablesTests(unittest.IsolatedAsyncioTestCase):

  async def test_happy_contract_passes(self):
    c = contract.parse_contract(INSTRUCTION, workspace_root="/app/files")
    files = {
        "/app/files/output/Witness_Scope_Memo.docx": oxml_bytes("docx"),
        "/app/files/witness_register.csv":
            b"Circuit,Length_ft,Amps\nPanel LP-1,10,5\nPanel LP-2,20,7\n",
    }
    report = await contract.validate_deliverables(c, MemIO(files), "/app/files")
    self.assertTrue(report.passed, report.to_dict())
    self.assertEqual(report.failures, [])

  async def test_missing_wrong_and_empty_files_fail(self):
    c = contract.parse_contract(
        "Write the memo to /app/out/Memo.docx and the data to `/app/out/d.csv`.",
        workspace_root="/app")
    files = {"/app/out/Memo.docx": b"", "/app/out/d.csv": b"x,y\n1,2\n"}
    report = await contract.validate_deliverables(c, MemIO(files), "/app")
    names = [(r.name, r.path, r.ok) for r in report.results]
    self.assertIn(("file_non_empty", "/app/out/Memo.docx", False), names)
    # the empty file fails before format sniffing; the csv passes its checks
    self.assertTrue(all(r.ok for r in report.results if r.path == "/app/out/d.csv"))
    self.assertFalse(report.passed)

  async def test_wrong_output_path_fails_as_missing(self):
    c = contract.parse_contract("Write the memo to /app/out/Memo.docx.",
                                workspace_root="/app")
    report = await contract.validate_deliverables(
        c, MemIO({"/app/out/Typo_Memo.docx": oxml_bytes()}), "/app")
    self.assertFalse(report.passed)
    self.assertIn("missing", report.failures[0].detail)

  async def test_missing_register_row_fails_confirmed_contract(self):
    c = contract.parse_contract(
        "Export `reg.csv` with columns: Zone, Area_sf. The register must include a row for Zone 3.")
    files = {"/app/reg.csv": b"Zone,Area_sf\nZone 1,10\n"}
    report = await contract.validate_deliverables(c, MemIO(files), "/app")
    self.assertFalse(report.passed)
    self.assertTrue(any("Zone 3" in r.detail for r in report.results))

  async def test_inferred_requirements_only_warn(self):
    c = contract.parse_contract("Mentioned: /app/out/extra.pdf nowhere required.")
    report = await contract.validate_deliverables(c, MemIO({}), "/app")
    self.assertTrue(report.passed)  # inferred misses are warnings
    self.assertTrue(any(r.severity == "warn" and not r.ok for r in report.results))

  async def test_no_confirmed_files_notes_but_passes(self):
    report = await contract.validate_deliverables(
        contract.parse_contract("Analyze the beam."), MemIO({}), "/app")
    self.assertTrue(report.passed)
    self.assertTrue(report.notes)

  async def test_validation_of_unreadable_file(self):
    class ExplodingIO(MemIO):
      async def read_bytes(self, path):
        raise PermissionError("denied")

    c = contract.parse_contract("Write the memo to /app/out/Memo.docx.")
    report = await contract.validate_deliverables(c, ExplodingIO({}), "/app")
    self.assertFalse(report.passed)
    self.assertTrue(any(r.name == "file_readable" and not r.ok for r in report.results))

  async def test_contract_dict_round_trip(self):
    c = contract.parse_contract(INSTRUCTION)
    as_dict = c.to_dict()
    self.assertTrue(as_dict["files"])
    self.assertEqual(as_dict["csv_schemas"], c.csv_schemas)


if __name__ == "__main__":
  unittest.main()
