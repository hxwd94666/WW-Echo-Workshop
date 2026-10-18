# 验证 Rust 制品与唯一源码对应且安装包不包含开发源码缓存。
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from echo_workshop.application.compute_protocol import ComputeFailure, verify_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))


class TestNativePackage(unittest.TestCase):
    def test_manifest_source_identity_and_archive_inventory(self):
        build = importlib.import_module('build')
        native = build.native_members()
        expected = build.contents(native)
        exe_name = 'echo_workshop/native/bin/echo-workshop-compute.exe'
        metadata = json.loads(native['echo_workshop/native/manifest.json'])
        self.assertEqual(hashlib.sha256(native[exe_name]).hexdigest(), metadata['sha256'])
        self.assertFalse(any('/native/compute/' in name or '/target/' in name for name in expected))
        self.assertTrue(native[exe_name].startswith(b'MZ'))
        self.assertIn('src/scoring.rs', metadata['sources'])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = root / 'compute.exe'
            manifest = root / 'manifest.json'
            engine.write_bytes(native[exe_name])
            manifest.write_bytes(native['echo_workshop/native/manifest.json'])
            self.assertEqual(metadata, verify_engine(engine, manifest))
            engine.write_bytes(native[exe_name] + b'changed')
            with self.assertRaises(ComputeFailure) as error:
                verify_engine(engine, manifest)
            self.assertEqual('engine_integrity', error.exception.kind)
