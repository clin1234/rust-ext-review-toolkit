"""Tests for measure_rust_complexity — per-function complexity scoring."""

import unittest

from fixtures import MINIMAL_PYO3_CRATE
from helpers import TempRustExtension, import_script

sub = import_script("measure_rust_complexity")


def run(code: str, **kw) -> dict:
    with TempRustExtension({"src/lib.rs": code}, **kw) as ext:
        return sub.analyze(str(ext.root))


# Five `unsafe` blocks add +5.0 to the score, clearing the 5.0 hotspot
# threshold regardless of length.
_COMPLEX = """\
fn risky() {
    unsafe { a(); }
    unsafe { b(); }
    unsafe { c(); }
    unsafe { d(); }
    unsafe { e(); }
}
"""

_SIMPLE = """\
fn add(a: i64, b: i64) -> i64 {
    a + b
}
"""


class TestComplexityScoring(unittest.TestCase):
    def test_complex_function_is_a_hotspot(self):
        report = run(_COMPLEX)
        findings = report["findings"]
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["type"], "complex_function")
        self.assertEqual(f["classification"], "CONSIDER")
        self.assertEqual(f["function"], "risky")
        self.assertGreaterEqual(f["details"]["score"], 5.0)
        self.assertEqual(f["details"]["unsafe_blocks"], 5)

    def test_simple_function_is_not_a_hotspot(self):
        report = run(_SIMPLE)
        self.assertEqual(report["findings"], [])

    def test_complexity_stats_present(self):
        report = run(MINIMAL_PYO3_CRATE)
        stats = report["complexity_stats"]
        self.assertEqual(stats["functions_scored"], 2)  # `add` and `minimal`
        self.assertEqual(stats["hotspots"], 0)
        self.assertEqual(stats["hotspot_threshold"], 5.0)
        self.assertGreaterEqual(stats["max_score"], 0.0)

    def test_hotspots_ranked_by_score(self):
        report = run(_COMPLEX + "\n" + _SIMPLE)
        scores = [f["details"]["score"] for f in report["findings"]]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main()
