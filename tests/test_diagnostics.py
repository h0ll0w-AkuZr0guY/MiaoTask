import importlib.metadata
import unittest
from unittest.mock import patch

from backend.core.service import diagnostics


class DistributionVersionTests(unittest.TestCase):
    def test_onnxruntime_falls_back_to_gpu_distribution(self):
        def version(name):
            if name == 'onnxruntime':
                raise importlib.metadata.PackageNotFoundError(name)
            return '1.31.1'

        with patch.object(importlib.metadata, 'version', side_effect=version):
            self.assertEqual(diagnostics._distribution_version('onnxruntime'), '1.31.1')

    def test_onnxruntime_prefers_the_installed_name(self):
        with patch.object(importlib.metadata, 'version', return_value='1.31.0') as lookup:
            self.assertEqual(diagnostics._distribution_version('onnxruntime'), '1.31.0')
        lookup.assert_called_once_with('onnxruntime')

    def test_missing_distribution_reports_every_candidate(self):
        with patch.object(importlib.metadata, 'version',
                          side_effect=importlib.metadata.PackageNotFoundError('missing')):
            with self.assertRaisesRegex(importlib.metadata.PackageNotFoundError,
                                        'onnxruntime or onnxruntime-gpu'):
                diagnostics._distribution_version('onnxruntime')


if __name__ == '__main__':
    unittest.main()
