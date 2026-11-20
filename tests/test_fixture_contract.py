from pathlib import Path
import csv
import unittest

ROOT = Path(__file__).resolve().parents[1]

class FixtureContractTests(unittest.TestCase):
    def test_each_csv_has_header_and_records(self):
        paths = sorted((ROOT / 'data').glob('*.csv'))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name), path.open(encoding='utf-8', newline='') as stream:
                rows = list(csv.DictReader(stream))
                self.assertTrue(rows)
                self.assertTrue(all(any(value.strip() for value in row.values()) for row in rows))

    def test_input_contract_exists(self):
        self.assertTrue((ROOT / 'docs' / 'input-contract.md').is_file())

if __name__ == '__main__':
    unittest.main()
