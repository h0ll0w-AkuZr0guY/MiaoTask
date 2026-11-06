"""Regression coverage for the DirectML runtime overlay manifest."""
import unittest

from backend.core.device.runtime_plugin import validate_manifest

BASE = dict(schema=1, kind='dml', version='1.0', tested_with='MiaoTask-v0.2.2',
            python='3.13', system='Windows', machine='amd64',
            runtime_version='1.24.4', packages={'onnxruntime-directml': '1.24.4'})
KW = dict(python_version='3.13', ort_version='1.31.0',
          system='Windows', machine='AMD64')


class DMLManifestTests(unittest.TestCase):
    def test_dml_manifest_accepts_independent_runtime_version(self):
        validate_manifest(dict(BASE), folder='dml', **KW)

    def test_dml_manifest_requires_runtime_version(self):
        manifest = dict(BASE)
        manifest.pop('runtime_version')
        with self.assertRaisesRegex(ValueError, 'runtime_version'):
            validate_manifest(manifest, folder='dml', **KW)

    def test_folder_must_match_kind(self):
        with self.assertRaisesRegex(ValueError, '目录'):
            validate_manifest(dict(BASE), folder='cuda', **KW)

    def test_cuda_still_requires_matching_base_ort(self):
        manifest = dict(BASE, kind='cuda', onnxruntime='1.31.0')
        validate_manifest(manifest, folder='cuda', **KW)
        manifest['onnxruntime'] = '1.24.4'
        with self.assertRaisesRegex(ValueError, 'onnxruntime'):
            validate_manifest(manifest, folder='cuda', **KW)

    def test_shared_fields_are_enforced(self):
        for key, value in (('python', '3.12'), ('machine', 'x86_64'), ('schema', 2)):
            with self.subTest(key=key):
                manifest = dict(BASE)
                manifest[key] = value
                with self.assertRaises(ValueError):
                    validate_manifest(manifest, folder='dml', **KW)


if __name__ == '__main__':
    unittest.main()
