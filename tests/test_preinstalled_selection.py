import unittest
from scripts.package_windows import selected_packages


class PreinstalledSelectionTests(unittest.TestCase):
    def test_release_selection_excludes_other_source_activities(self):
        catalog = {'packages': [dict(app='sample', activity=name) for name in ('daily', 'seasonal')]}
        self.assertEqual(selected_packages(catalog, ['sample/seasonal']), [catalog['packages'][1]])
        self.assertEqual(selected_packages(catalog, []), [])

    def test_unknown_and_repeated_selections_are_rejected(self):
        catalog = {'packages': [dict(app='sample', activity='daily')]}
        for choices in (['missing/daily'], ['sample/daily', 'sample/daily']):
            with self.assertRaises(ValueError):
                selected_packages(catalog, choices)
