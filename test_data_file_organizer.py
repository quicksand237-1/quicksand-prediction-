import csv
import tempfile
import unittest
from pathlib import Path

from data_file_organizer import consolidate_csv_candidates, delete_safe_duplicates, scan_data_files


class DataFileOrganizerTests(unittest.TestCase):
    def test_scan_and_consolidate_preserves_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            first = data / "first.csv"
            second = data / "second.csv"
            rows = [["slug", "checkpoint_utc", "value"], ["a", "2026-01-01T00:00:00Z", "1"], ["a", "2026-01-01T00:05:00Z", "2"]]
            for path in (first, second):
                with path.open("w", newline="", encoding="utf-8") as handle:
                    csv.writer(handle).writerows(rows)
            inventory = scan_data_files(data)
            self.assertEqual(len(inventory["exact_duplicate_groups"]), 1)
            result = consolidate_csv_candidates(inventory, data, data / "consolidated")
            self.assertTrue(result["sources_preserved"])
            self.assertEqual(first.read_text(encoding="utf-8"), second.read_text(encoding="utf-8"))
            output = data / "consolidated" / "001_first_consolidated.csv"
            self.assertTrue(output.exists())
            self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 3)

    def test_delete_only_low_risk_exact_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            data.mkdir()
            first = data / "observations.csv"
            second = data / "observations_copy.csv"
            content = "slug,checkpoint_utc\na,2026-01-01T00:00:00Z\n"
            first.write_text(content, encoding="utf-8")
            second.write_text(content, encoding="utf-8")
            expected_bytes = second.stat().st_size
            inventory = scan_data_files(data)
            result = delete_safe_duplicates(inventory, data)
            self.assertEqual(result["bytes_freed"], expected_bytes)
            self.assertTrue(first.exists())
            self.assertFalse(second.exists())


if __name__ == "__main__":
    unittest.main()
