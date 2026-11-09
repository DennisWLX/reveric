#!/usr/bin/env bash
# Build the vendored native runtimes and an Intel macOS DMG.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != x86_64 ]]; then
  echo 'This build requires an Intel Mac or the macos-15-intel Actions runner.' >&2
  exit 2
fi

if [[ "${GITHUB_ACTIONS:-}" == true ]]; then
  FLUTTER=(flutter)
  DART=(dart)
  FASTFORGE=(fastforge)
else
  FLUTTER=(fvm flutter)
  DART=(fvm dart)
  FASTFORGE=(fvm exec fastforge)
fi
export REVERIC_MACOS_ARCH=x86_64
export MACOSX_DEPLOYMENT_TARGET=12.0

# The SDK revision must match the experimental windowing APIs used by the app.
SDK_VERSION="$(mktemp "${TMPDIR:-/tmp}/reveric-intel-sdk.XXXXXX")"
PUBSPEC_BACKUP="$(mktemp "${TMPDIR:-/tmp}/reveric-intel-pubspec.XXXXXX")"
cp pubspec.yaml "$PUBSPEC_BACKUP"
cleanup() {
  cp "$PUBSPEC_BACKUP" "$ROOT/pubspec.yaml"
  rm -f "$PUBSPEC_BACKUP" "$SDK_VERSION"
}
trap cleanup EXIT
"${FLUTTER[@]}" --version --machine > "$SDK_VERSION"
python3 - "$SDK_VERSION" <<'PY'
import json
from pathlib import Path
import sys

expected = json.loads(Path('.fvmrc').read_text())['flutter']
actual = json.loads(Path(sys.argv[1]).read_text())['frameworkRevision']
if actual != expected:
    raise SystemExit(f'Flutter revision mismatch: expected {expected}, found {actual}')
path = Path('pubspec.yaml')
lines = path.read_text().splitlines(keepends=True)
in_hooks = False
for index, line in enumerate(lines):
    if line.startswith('hooks:'):
        in_hooks = True
    elif not line.startswith(' '):
        in_hooks = False
    if in_hooks:
        lines[index] = '# ' + line
path.write_text(''.join(lines))
PY
"${FLUTTER[@]}" pub get

# The official FJS SwiftPM archive only has an arm64 macOS slice. Compile the
# same vendored Rust sources and locked dependencies for Intel instead.
FJS_WORK="$ROOT/build/macos-intel/fjs"
export CARGO_TARGET_DIR="$ROOT/build/macos-intel/cargo"
cargo +1.99.1 build --locked --release --lib --target x86_64-apple-darwin \
  --manifest-path "$ROOT/third/fjs/libfjs/Cargo.toml"
mkdir -p "$FJS_WORK"
python3 - "$ROOT" "$FJS_WORK" "$CARGO_TARGET_DIR" <<'PY'
from pathlib import Path
import plistlib
import re
import shutil
import sys

root, work, target = map(Path, sys.argv[1:])
framework = work / 'fjs.framework'
contents = framework / 'Versions/A'
for directory in ('Headers', 'Modules', 'Resources'):
    (contents / directory).mkdir(parents=True, exist_ok=True)
shutil.copy2(target / 'x86_64-apple-darwin/release/libfjs.dylib', contents / 'fjs')
(contents / 'Headers/fjs.h').write_text('#pragma once\n')
(contents / 'Modules/module.modulemap').write_text(
    'framework module fjs {\n  umbrella header "fjs.h"\n  export *\n  module * { export * }\n}\n')
version = re.search(r'^version:\s*(\S+)', (root / 'third/fjs/pubspec.yaml').read_text(), re.M).group(1)
with (contents / 'Resources/Info.plist').open('wb') as stream:
    plistlib.dump({
        'CFBundleExecutable': 'fjs',
        'CFBundleIdentifier': 'dev.fluttercandies.fjs.macos',
        'CFBundleInfoDictionaryVersion': '6.0',
        'CFBundleName': 'fjs',
        'CFBundlePackageType': 'FMWK',
        'CFBundleShortVersionString': version,
        'CFBundleVersion': version,
        'MinimumOSVersion': '12.0',
    }, stream)
for name, destination in {
    'Versions/Current': 'A', 'fjs': 'Versions/Current/fjs',
    'Headers': 'Versions/Current/Headers', 'Modules': 'Versions/Current/Modules',
    'Resources': 'Versions/Current/Resources',
}.items():
    link = framework / name
    if not link.is_symlink():
        link.symlink_to(destination)
PY
install_name_tool -id '@rpath/fjs.framework/fjs' "$FJS_WORK/fjs.framework/Versions/A/fjs"
codesign --force --sign - "$FJS_WORK/fjs.framework"
codesign --verify --strict "$FJS_WORK/fjs.framework"
"${DART[@]}" third/fjs/tool/check_frb_content_hash.dart "$FJS_WORK/fjs.framework/Versions/A/fjs"
if [[ -e "$FJS_WORK/fjs.xcframework" ]]; then rm -rf "$FJS_WORK/fjs.xcframework"; fi
xcodebuild -create-xcframework -framework "$FJS_WORK/fjs.framework" -output "$FJS_WORK/fjs.xcframework"
FJS_BINARIES="$ROOT/third/fjs/darwin/fjs/Binaries"
mkdir -p "$FJS_BINARIES"
ditto -c -k --sequesterRsrc --keepParent "$FJS_WORK/fjs.xcframework" "$FJS_BINARIES/fjs.xcframework.zip"
shasum -a 256 "$FJS_BINARIES/fjs.xcframework.zip" | awk '{print $1}' > "$FJS_BINARIES/fjs.xcframework.zip.checksum"

bash third/fvp/macos/setup.sh
bash third/depth-native/prepare-product-macos.sh
"${FASTFORGE[@]}" release --name release --jobs macos-intel-dmg --skip-clean
APP="$ROOT/build/macos/Build/Products/Release/Reveric.app"
python3 macos/architectures.py --arch x86_64 "$APP"
codesign --verify --deep --strict "$APP"
"${DART[@]}" third/fjs/tool/check_frb_content_hash.dart "$APP/Contents/Frameworks/fjs.framework/fjs"

DMG="$(find "$ROOT/dist" -type f -name '*-macos-x86_64.dmg' -print -quit)"
[[ -n "$DMG" ]] || { echo 'Intel DMG was not produced.' >&2; exit 1; }
(cd "$(dirname "$DMG")" && shasum -a 256 "$(basename "$DMG")") > "$DMG.sha256"
python3 - "$DMG" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

artifact = Path(sys.argv[1])
artifact.with_suffix('.build.json').write_text(json.dumps({
    'architecture': 'x86_64',
    'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
    'flutter_revision': json.loads(Path('.fvmrc').read_text())['flutter'],
    'rust_toolchain': '1.99.1',
    'xcode': subprocess.check_output(['xcodebuild', '-version'], text=True).strip(),
    'artifact': artifact.name,
}, indent=2) + '\n')
PY
echo "Intel DMG: $DMG"
