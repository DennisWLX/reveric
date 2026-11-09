#!/usr/bin/env python3
"""Prepare embedded frameworks and verify the requested macOS architecture."""

import argparse
from pathlib import Path
import os
import shutil
import subprocess
import tempfile


def verify(app: Path, prepare=False, architecture="arm64"):
    if architecture not in ("arm64", "x86_64"):
        raise SystemExit(f"Unsupported macOS architecture: {architecture}")
    if not (app / "Contents/MacOS").is_dir():
        raise SystemExit(f"Not a macOS app bundle: {app}")
    # Mach-O and universal headers, in both byte orders.
    magic = {bytes.fromhex(value) for value in (
        "feedface", "cefaedfe", "feedfacf", "cffaedfe",
        "cafebabe", "bebafeca", "cafebabf", "bfbafeca",
    )}
    count = 0
    frameworks = app / "Contents/Frameworks"
    changed_frameworks = set()
    identity = os.environ.get("EXPANDED_CODE_SIGN_IDENTITY") or "-"

    def sign(path):
        subprocess.run([
            "codesign", "--force", "--sign", identity,
            "--preserve-metadata=identifier,entitlements,flags,runtime", str(path),
        ], check=True)

    for binary in app.rglob("*"):
        if binary.is_symlink() or not binary.is_file():
            continue
        with binary.open("rb") as source:
            if source.read(4) not in magic:
                continue
        architectures = subprocess.check_output(
            ["lipo", "-archs", str(binary)], text=True,
        ).split()
        if prepare and architectures != [architecture] and binary.is_relative_to(frameworks):
            if architecture not in architectures:
                raise SystemExit(f"Framework has no {architecture} slice: {binary}")
            with tempfile.TemporaryDirectory(prefix=f".{architecture}-", dir=binary.parent) as temp:
                thin = Path(temp) / binary.name
                subprocess.run([
                    "lipo", str(binary), "-thin", architecture, "-output", str(thin),
                ], check=True)
                shutil.copystat(binary, thin)
                thin.replace(binary)
            sign(binary)
            changed_frameworks.update(
                parent for parent in binary.parents
                if parent.suffix == ".framework" and parent.is_relative_to(frameworks)
            )
            architectures = subprocess.check_output(
                ["lipo", "-archs", str(binary)], text=True,
            ).split()
        if architectures != [architecture]:
            raise SystemExit(f"Expected {architecture}, found {architectures}: {binary}")
        count += 1
    if not count:
        raise SystemExit(f"No native binaries found: {app}")
    # Seal enclosing frameworks after their nested libraries have been signed.
    # Xcode signs the enclosing app after its embed build phase completes.
    for framework in sorted(changed_frameworks, key=lambda p: len(p.parts), reverse=True):
        sign(framework)
    print(f"Verified {count} {architecture} native binaries: {app}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--arch", choices=("arm64", "x86_64"),
                        default=os.environ.get("REVERIC_MACOS_ARCH", "arm64"))
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    verify(args.app, prepare=args.prepare, architecture=args.arch)
