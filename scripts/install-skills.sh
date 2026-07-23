#!/usr/bin/env bash
#
# install-skills.sh — Deploy-time community-skill installer for Sprout.
#
# Runs BEFORE `docker build` (never inside the Dockerfile). It reads
# agent-container/community-skills.json ({"skills": ["name1", "name2", ...]}),
# installs each named skill from ClawHub into agent-container/skills/<name>/, and
# registers it in agent-container/openclaw.json under skills.entries so the
# OpenClaw gateway loads it at runtime.
#
# Resolution strategy per skill:
#   1. If the `openclaw` CLI is available on PATH, try
#      `openclaw skill install <name> --skills-dir agent-container/skills`.
#   2. Otherwise (or on failure) WARN and continue, ensuring a minimal local
#      stub agent-container/skills/<name>/SKILL.md exists so the image still
#      builds with a loadable (if inert) skill.
#
# The script is idempotent: re-running it re-registers skills without
# duplicating entries and leaves existing stubs/real skills in place.
#
set -euo pipefail

log() { printf '%s\n' "$*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
AGENT_DIR="${REPO_ROOT}/agent-container"
SKILLS_FILE="${AGENT_DIR}/community-skills.json"
SKILLS_DIR="${AGENT_DIR}/skills"
CONFIG_FILE="${AGENT_DIR}/openclaw.json"

if [[ ! -f "$SKILLS_FILE" ]]; then
  log "No community-skills.json at ${SKILLS_FILE}; nothing to install."
  exit 0
fi
if [[ ! -f "$CONFIG_FILE" ]]; then
  log "ERROR: openclaw.json not found at ${CONFIG_FILE}."
  exit 1
fi

mkdir -p "$SKILLS_DIR"

# Read the skill names from the JSON ({"skills": [...]}) using python3. A
# newline-delimited list keeps this portable to bash 3.2 (macOS default), which
# lacks `mapfile`.
SKILLS=()
while IFS= read -r name; do
  [[ -z "$name" ]] && continue
  SKILLS+=("$name")
done < <(python3 -c "import json; print('\n'.join(json.load(open('${SKILLS_FILE}')).get('skills', [])))")

if [[ "${#SKILLS[@]}" -eq 0 ]]; then
  log "community-skills.json declares no skills; nothing to install."
  exit 0
fi

log "==> Installing ${#SKILLS[@]} community skill(s) into ${SKILLS_DIR}"

for name in "${SKILLS[@]}"; do
  [[ -z "$name" ]] && continue
  dest="${SKILLS_DIR}/${name}"
  installed=false

  if command -v openclaw >/dev/null 2>&1; then
    log "Resolving ClawHub skill '${name}' via openclaw CLI..."
    if openclaw skill install "$name" --skills-dir "$SKILLS_DIR" >&2; then
      installed=true
      log "Installed skill '${name}' from ClawHub."
    else
      log "WARN: 'openclaw skill install ${name}' failed; falling back to a local stub."
    fi
  else
    log "WARN: openclaw CLI not on PATH; cannot resolve ClawHub skill '${name}'. Using a local stub."
  fi

  if [[ "$installed" != true ]]; then
    mkdir -p "$dest"
    if [[ ! -f "${dest}/SKILL.md" ]]; then
      cat > "${dest}/SKILL.md" <<EOF
# ${name} skill (local stub)

Auto-generated fallback stub for the '${name}' skill because it could not be
resolved from ClawHub at deploy time. Replace with the real skill when available.
EOF
      log "Created local stub ${dest}/SKILL.md"
    else
      log "Local stub already present at ${dest}/SKILL.md; leaving it in place."
    fi
  fi
done

# Register every declared skill in openclaw.json under skills.entries (enabled).
log "==> Registering skills in ${CONFIG_FILE}"
python3 - "$CONFIG_FILE" "$SKILLS_FILE" <<'PY'
import json
import sys

config_path, skills_path = sys.argv[1], sys.argv[2]

with open(config_path) as f:
    config = json.load(f)
with open(skills_path) as f:
    names = json.load(f).get("skills", [])

skills = config.setdefault("skills", {})
entries = skills.setdefault("entries", {})
# Keep the built-in memory skill enabled alongside the community skills.
entries.setdefault("memory", {"enabled": True})
for name in names:
    if not name:
        continue
    entry = entries.setdefault(name, {})
    entry["enabled"] = True

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")

print(f"Registered {len([n for n in names if n])} skill(s) in openclaw.json", file=sys.stderr)
PY

log "Community skill installation complete."
