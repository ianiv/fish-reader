#!/usr/bin/env bash
# Quality gate: compile the server, run the smoke test (fake model + fake tagger), build + test the iOS app
# when Xcode and xcodegen are present (SKIP_IOS=1 skips it).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="${FISH_READER_PYTHON:-$HERE/.venv/bin/python}"
cd "$HERE"
echo "== py_compile"
"$PY" -m py_compile server/*.py scripts/*.py
/usr/bin/python3 -m py_compile scripts/setup.py scripts/common.py  # setup runs on macOS's Python 3.9
echo "== smoke"
"$PY" scripts/smoke.py 2>&1 | grep -v '^\[' 
if [ "${SKIP_IOS:-}" != "1" ] && command -v xcodebuild >/dev/null && command -v xcodegen >/dev/null; then
  echo "== ios"
  cd ios && xcodegen generate --quiet
  if [ -n "${IOS_DESTINATION:-}" ]; then  # e.g. IOS_DESTINATION="platform=iOS Simulator,name=iPhone 17 Pro"
    xcodebuild -quiet -project ArticleReader.xcodeproj -scheme ArticleReader -destination "$IOS_DESTINATION" test
  else  # compile app, extension and tests without booting a simulator
    xcodebuild -quiet -project ArticleReader.xcodeproj -scheme ArticleReader -destination 'generic/platform=iOS Simulator' \
      -derivedDataPath build build-for-testing 2>&1 | grep -v appintentsmetadataprocessor || true
    test -f build/Build/Products/Debug-iphonesimulator/ArticleReader.app/PlugIns/ReadAloud.appex/GetPage.js
  fi
  cd ..
else
  echo "== ios: skipped"
fi
echo "check: ok"
