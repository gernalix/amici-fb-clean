#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
project_dir="$(cd -- "${script_dir}/.." && pwd -P)"
python_bin="${PYTHON_BIN:-/usr/bin/python3}"

site_packages="$("${python_bin}" - <<'PY'
import sysconfig
print(sysconfig.get_paths()["purelib"])
PY
)"

sudo install -d -m 0755 "${site_packages}/telegram_notify"
sudo install -m 0644 "${project_dir}/telegram_notify/"*.py "${site_packages}/telegram_notify/"
cd /
"${python_bin}" - <<'PY'
import telegram_notify
print(telegram_notify.__file__)
print(telegram_notify.config_summary())
PY
