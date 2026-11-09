"""Exercise the packaging architecture check with real Mach-O binaries."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "macos/architectures.py"


@unittest.skipUnless(sys.platform == "darwin", "Requires Apple linker and codesign")
class ArchitectureTests(unittest.TestCase):
    architecture = "arm64"

    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix="reveric-macho-")
        cls.root = Path(cls.fixture.name)
        source = cls.root / "probe.c"
        source.write_text("int main(void) { return 0; }\n")
        for architecture in ("arm64", "x86_64"):
            subprocess.run([
                "xcrun", "clang", "-arch", architecture, str(source),
                "-o", str(cls.root / f"Runner-{architecture}"),
            ], check=True, capture_output=True)
        subprocess.run([
            "xcrun", "clang", "-dynamiclib", "-arch", "arm64", "-arch", "x86_64",
            str(source), "-o", str(cls.root / "libprobe.dylib"),
        ], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=f"reveric-{self.architecture}-app-")
        self.addCleanup(self.temp.cleanup)
        self.app = Path(self.temp.name) / "Fixture.app"
        (self.app / "Contents/MacOS").mkdir(parents=True)
        (self.app / "Contents/Frameworks").mkdir()
        shutil.copy2(self.root / f"Runner-{self.architecture}", self.app / "Contents/MacOS/Runner")
        self.library = self.app / "Contents/Frameworks/libprobe.dylib"
        shutil.copy2(self.root / "libprobe.dylib", self.library)

    def run_check(self, prepare=False):
        return subprocess.run([
            sys.executable, str(SCRIPT), *(["--prepare"] if prepare else []),
            "--arch", self.architecture, str(self.app),
        ], capture_output=True, text=True)

    def test_verification_rejects_universal_library(self):
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected " + self.architecture, result.stderr)

    def test_preparation_thins_and_signs_then_verification_passes(self):
        executable = self.app / "Contents/MacOS/Runner"
        original = executable.read_bytes()
        result = self.run_check(prepare=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(executable.read_bytes(), original)
        subprocess.run(["codesign", "--verify", "--strict", str(self.library)], check=True)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.library.read_bytes()
        self.assertEqual(self.run_check(prepare=True).returncode, 0)
        self.assertEqual(self.library.read_bytes(), before)

    def test_preparation_rejects_library_without_requested_architecture(self):
        other = "x86_64" if self.architecture == "arm64" else "arm64"
        subprocess.run([
            "lipo", str(self.root / "libprobe.dylib"), "-thin", other,
            "-output", str(self.library),
        ], check=True)
        result = self.run_check(prepare=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"no {self.architecture} slice", result.stderr)

    def test_preparation_rejects_executable_for_other_architecture(self):
        other = "x86_64" if self.architecture == "arm64" else "arm64"
        shutil.copy2(self.root / f"Runner-{other}", self.app / "Contents/MacOS/Runner")
        result = self.run_check(prepare=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected " + self.architecture, result.stderr)

    def test_preparation_uses_architecture_from_environment(self):
        result = subprocess.run([
            sys.executable, str(SCRIPT), "--prepare", str(self.app),
        ], env={**os.environ, "REVERIC_MACOS_ARCH": self.architecture},
           capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{self.architecture} native binaries", result.stdout)


class IntelArchitectureTests(ArchitectureTests):
    architecture = "x86_64"


if __name__ == "__main__":
    unittest.main()
