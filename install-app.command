#!/bin/zsh
set -eu

repo_dir=${0:A:h}
launcher="$repo_dir/Babel.command"
[[ -x "$launcher" ]] || { print -u2 "Babel.command is missing or not executable: $launcher"; exit 1; }

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/mbabel-install.XXXXXX")
trap 'rm -rf "$tmp_dir"' EXIT
shell_command=$(printf '%q' "$launcher")
apple_command=${shell_command//\\/\\\\}
apple_command=${apple_command//\"/\\\"}

osacompile -o "$tmp_dir/mBabel.app" \
  -e 'on run' \
  -e 'tell application "Terminal"' \
  -e "do script \"$apple_command; exit\"" \
  -e 'activate' \
  -e 'end tell' \
  -e 'end run'

# App icon: build applet.icns from the repo master with stock macOS tools.
# Cosmetic — a failure here must not abort the install.
icon_png="$repo_dir/assets/icon.png"
if [[ -f "$icon_png" ]]; then
  iconset="$tmp_dir/mBabel.iconset"
  mkdir "$iconset"
  icon_ok=1
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$icon_png" --out "$iconset/icon_${size}x${size}.png" >/dev/null \
      && sips -z $((size * 2)) $((size * 2)) "$icon_png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null \
      || icon_ok=0
  done
  { (( icon_ok )) \
      && iconutil -c icns "$iconset" -o "$tmp_dir/mBabel.app/Contents/Resources/applet.icns" \
      && rm -f "$tmp_dir/mBabel.app/Contents/Resources/Assets.car" \
      && { /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" \
             "$tmp_dir/mBabel.app/Contents/Info.plist" 2>/dev/null || true; }; } \
    || print -u2 "warning: could not build the app icon; keeping the default"
  # IconServices prefers the asset catalog over CFBundleIconFile, so the
  # template's Assets.car/CFBundleIconName must go for applet.icns to win.
  # Editing the bundle breaks osacompile's seal: re-seal ad hoc.
  codesign --force --sign - "$tmp_dir/mBabel.app" 2>/dev/null \
    || print -u2 "warning: could not re-sign the app"
fi

# ditto merges into an existing bundle and would leave stale template
# files (e.g. Assets.car) behind: replace the old install outright.
install_dir=/Applications
if [[ -d "$install_dir" && -w "$install_dir" ]] \
    && rm -rf "$install_dir/mBabel.app" \
    && /usr/bin/ditto "$tmp_dir/mBabel.app" "$install_dir/mBabel.app"; then
  :
else
  install_dir="$HOME/Applications"
  mkdir -p "$install_dir"
  rm -rf "$install_dir/mBabel.app"
  /usr/bin/ditto "$tmp_dir/mBabel.app" "$install_dir/mBabel.app"
fi
touch "$install_dir/mBabel.app"  # nudge Finder/LaunchServices to refresh the icon
print "Installed $install_dir/mBabel.app"
