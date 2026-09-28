#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
label="com.parham.portfolio-backup-pull"
agent="${HOME}/Library/LaunchAgents/${label}.plist"
log_dir="${HOME}/Library/Logs"
support_dir="${HOME}/Library/Application Support/portfolio-backup"
installed_script="${support_dir}/pull_offhost_backup.sh"
mkdir -p "${HOME}/Library/LaunchAgents" "${log_dir}" "${support_dir}"
# Background agents cannot execute code directly from Downloads on macOS.
install -m 700 "${script_dir}/pull_offhost_backup.sh" "${installed_script}"
python3 - "${agent}" "${label}" "${installed_script}" "${log_dir}" <<'PY'
import plistlib
import sys

path, label, script, logs = sys.argv[1:]
with open(path, "wb") as output:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": [script],
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True,
        "StartInterval": 3600,
        "StandardOutPath": f"{logs}/{label}.log",
        "StandardErrorPath": f"{logs}/{label}.error.log",
    }, output)
PY
domain="gui/$(id -u)"
launchctl bootout "${domain}/${label}" 2>/dev/null || true
launchctl bootstrap "${domain}" "${agent}"
printf 'Installed hourly Mac backup pull: %s\n' "${agent}"
