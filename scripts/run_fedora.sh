#!/usr/bin/env bash
set -euo pipefail

script_path="$(readlink -f -- "${BASH_SOURCE[0]}")"
script_dir="$(dirname -- "${script_path}")"
project_dir="$(cd -- "${script_dir}/.." && pwd -P)"
python_bin="${PYTHON_BIN:-/usr/bin/python3}"

if [[ ! -x "${python_bin}" ]]; then
  echo "ERROR python not executable: ${python_bin}" >&2
  exit 127
fi

args=("$@")
login_mode=0
for arg in "${args[@]:-}"; do
  if [[ "${arg}" == "--login" ]]; then
    login_mode=1
  fi
done

if [[ ${#args[@]} -eq 0 ]]; then
  args=(--headless)
fi

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export TELEGRAM_NOTIFY_CONFIG="${TELEGRAM_NOTIFY_CONFIG:-$HOME/.config/codex/secrets/telegram.env}"
export AMICI_FB_STORAGE_STATE="${AMICI_FB_STORAGE_STATE:-$HOME/.local/share/amici_fb/fb_storage_state.json}"
export AMICI_FB_CHROMIUM_PROFILE="${AMICI_FB_CHROMIUM_PROFILE:-$HOME/.local/share/amici_fb/chromium-profile}"
export AMICI_FB_OUTPUT_DIR="${AMICI_FB_OUTPUT_DIR:-$HOME/.local/share/amici_fb/data}"

if [[ "${login_mode}" -eq 0 ]]; then
  unset DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS DESKTOP_SESSION XDG_CURRENT_DESKTOP
fi

mkdir -p "${project_dir}/data" "$(dirname -- "${AMICI_FB_STORAGE_STATE}")" "${AMICI_FB_CHROMIUM_PROFILE}"

cd "${project_dir}"
exec "${python_bin}" -u -X faulthandler "${project_dir}/amici_fb_task_runner.py" "${args[@]}"
