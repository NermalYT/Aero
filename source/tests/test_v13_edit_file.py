"""1.3: edit_file keeps line endings and copes with how models copy snippets (found in a live mod: ten "not found"
in a row on a CRLF file with long lines).
Run from source/:
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

if "aero.config" not in sys.modules:
    os.environ["AERO_HOME"] = tempfile.mkdtemp(prefix="aero-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aero import tools  # noqa: E402
from aero.tools import files  # noqa: E402

JS = "function a() {\r\n  const x = 1;\r\n  return x;\r\n}\r\n\r\nfunction b() {\r\n  return 2;\r\n}\r\n"


class EditFile(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.f = self.d / "app.js"
        self.f.write_bytes(JS.encode("utf-8"))
        self.ctx = tools.Ctx({"work_dir": str(self.d)})

    def text(self):
        return self.f.read_bytes().decode("utf-8")

    def test_crlf_files_stay_crlf(self):
        r = files.edit_file(self.ctx, "app.js", "  return 2;", "  return 3;")
        self.assertIn("Edited", r)
        self.assertEqual(self.text(), JS.replace("return 2;", "return 3;"))       # every other line untouched

    def test_lf_snippets_match_crlf_files(self):
        files.edit_file(self.ctx, "app.js", "  const x = 1;\n  return x;", "  const x = 5;\n  return x;")
        self.assertIn("const x = 5;\r\n  return x;", self.text())

    def test_copied_line_numbers_are_ignored(self):
        r = files.edit_file(self.ctx, "app.js", "    7    return 2;", "    7    return 7;")
        self.assertIn("Edited", r)
        self.assertIn("  return 7;\r\n", self.text())

    def test_indentation_differences_when_there_is_one_place(self):
        r = files.edit_file(self.ctx, "app.js", "const x = 1;\nreturn x;", "const x = 9;\nreturn x * 2;")
        self.assertIn("ignoring indentation", r)
        self.assertIn("  const x = 9;\r\n  return x * 2;\r\n", self.text())          # the file's indentation kept

    def test_ambiguous_loose_matches_are_refused(self):
        self.f.write_bytes(b"  return 2;\n    return 2;\n")
        r = files.edit_file(self.ctx, "app.js", "\treturn 2;", "\treturn 3;")
        self.assertTrue(r["error"])
        self.assertIn("2 places", r["text"])

    def test_not_found_points_at_the_closest_line(self):
        r = files.edit_file(self.ctx, "app.js", "const y = 1;", "const y = 2;")
        self.assertTrue(r["error"])
        self.assertIn("closest line is 2", r["text"])
        self.assertEqual(self.text(), JS)

    def test_mixed_endings_leave_other_lines_alone(self):
        mixed = "a = 1\r\nb = 2\nc = 3\r\n"
        self.f.write_bytes(mixed.encode("utf-8"))
        self.assertIn("Edited", files.edit_file(self.ctx, "app.js", "b = 2", "b = 5"))
        self.assertEqual(self.text(), "a = 1\r\nb = 5\nc = 3\r\n")
        self.assertIn("Edited", files.edit_file(self.ctx, "app.js", "a = 1\nb = 5", "a = 0\nb = 5"))
        self.assertTrue(self.text().startswith("a = 0"))
        self.assertTrue(files.edit_file(self.ctx, "app.js", "  c = 3", "x")["error"])   # no loose matching here

    def test_a_loose_deletion_leaves_no_blank_line(self):
        r = files.edit_file(self.ctx, "app.js", "const x = 1;\nreturn x;", "")
        self.assertIn("ignoring indentation", r)
        self.assertEqual(self.text(), "function a() {\r\n}\r\n\r\nfunction b() {\r\n  return 2;\r\n}\r\n")

    def test_lines_that_really_start_with_numbers(self):
        self.f.write_bytes(b"10  apples\n20  pears\n")
        self.assertIn("Edited", files.edit_file(self.ctx, "app.js", "20  pears", "20  plums"))
        self.assertEqual(self.text(), "10  apples\n20  plums\n")

    def test_exact_edits_and_replace_all_as_before(self):
        self.assertIn("2 replacements", files.edit_file(self.ctx, "app.js", "function", "fn", replace_all=True))
        self.assertTrue(files.edit_file(self.ctx, "app.js", "return", "x")["error"])   # 3 matches, no replace_all


if __name__ == "__main__":
    unittest.main()
