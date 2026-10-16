#!/usr/bin/env bash
set -euo pipefail

readonly config_path="${TELEGRAM_NOTIFY_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/codex/secrets/telegram.env}"

if [[ ! -r "${config_path}" ]]; then
  printf 'ERROR canonical Telegram config is missing or unreadable: %s\n' "${config_path}" >&2
  exit 66
fi
if [[ "$(stat -c '%u' -- "${config_path}")" != "$(id -u)" || \
      "$(stat -c '%a' -- "${config_path}")" != "600" ]]; then
  printf 'ERROR canonical Telegram config must be owned by the current user with mode 600.\n' >&2
  exit 77
fi
if ! grep -Eq '^TELEGRAM_INSERT_BOT_NOISY_BOT_TOKEN=.+$' "${config_path}" || \
   ! grep -Eq '^TELEGRAM_INSERT_BOT_NOISY_CHAT_ID=.+$' "${config_path}"; then
  printf 'ERROR canonical Telegram config lacks required fields.\n' >&2
  exit 78
fi
printf 'Telegram config canonica valida: %s\n' "${config_path}"
