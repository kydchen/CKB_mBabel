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

install_dir=/Applications
if [[ -d "$install_dir" && -w "$install_dir" ]] \
    && /usr/bin/ditto "$tmp_dir/mBabel.app" "$install_dir/mBabel.app"; then
  :
else
  install_dir="$HOME/Applications"
  mkdir -p "$install_dir"
  /usr/bin/ditto "$tmp_dir/mBabel.app" "$install_dir/mBabel.app"
fi
print "Installed $install_dir/mBabel.app"
