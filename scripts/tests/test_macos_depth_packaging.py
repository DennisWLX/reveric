"""Verify that a packaged depth SDK contains the requested native architecture."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "third/depth-native/package_reduced.py"
LIBRARY = "libonnxruntime.1.31.0.dylib"


@unittest.skipUnless(sys.platform == "darwin", "Requires Apple linker and codesign")
class DepthPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix="reveric-depth-fixture-")
        cls.root = Path(cls.fixture.name)
        source = cls.root / "probe.c"
        source.write_text("int depth_probe(void) { return 0; }\n")
        for architecture in ("arm64", "x86_64"):
            subprocess.run([
                "xcrun", "clang", "-dynamiclib", "-arch", architecture,
                str(source), "-o", str(cls.root / f"{architecture}.dylib"),
            ], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reveric-depth-package-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        reference = self.work / "onnxruntime-osx-arm64-1.31.0"
        (reference / "include").mkdir(parents=True)
        (reference / "include/onnxruntime_c_api.h").write_text("/* fixture */\n")
        (reference / "lib").mkdir()
        shutil.copy2(self.root / "arm64.dylib", reference / "lib" / LIBRARY)
        for name in ("LICENSE", "ThirdPartyNotices.txt"):
            (reference / name).write_text(name + "\n")

    def package(self, architecture, binary_architecture=None):
        suffix = "-x86_64" if architecture == "x86_64" else ""
        reduced = self.work / ("ort-reduced-1.31.0" + suffix)
        build = reduced / "build/MinSizeRel"
        build.mkdir(parents=True)
        shutil.copy2(self.root / f"{binary_architecture or architecture}.dylib", build / LIBRARY)
        result = subprocess.run([
            sys.executable, str(SCRIPT), str(self.work), "--arch", architecture,
        ], capture_output=True, text=True)
        return result, reduced

    def test_intel_package_contains_intel_runtime_and_shared_headers(self):
        result, reduced = self.package("x86_64")
        self.assertEqual(result.returncode, 0, result.stderr)
        library = reduced / "dist/lib/libonnxruntime.dylib"
        self.assertEqual(subprocess.check_output(["lipo", "-archs", str(library)], text=True).strip(), "x86_64")
        subprocess.run(["codesign", "--verify", "--strict", str(library)], check=True)
        self.assertTrue((reduced / "dist/include/onnxruntime_c_api.h").is_file())
        self.assertTrue((reduced / "dist/ThirdPartyNotices.txt").is_file())
        report = json.loads((reduced / "size-report.json").read_text())
        self.assertEqual(report["platform"], "macOS x86_64")
        self.assertNotIn("reduction_percent", report)

    def test_arm64_package_keeps_same_architecture_size_comparison(self):
        result, reduced = self.package("arm64")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((reduced / "size-report.json").read_text())
        self.assertEqual(report["platform"], "macOS arm64")
        self.assertIn("reduction_percent", report)
        self.assertIn("full_stripped", report)

    def test_rejects_runtime_from_other_architecture_before_packaging(self):
        for architecture, other in (("arm64", "x86_64"), ("x86_64", "arm64")):
            with self.subTest(architecture=architecture):
                result, reduced = self.package(architecture, other)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Expected {architecture} depth runtime", result.stderr)
                self.assertFalse((reduced / "dist").exists())


if __name__ == "__main__":
    unittest.main()
