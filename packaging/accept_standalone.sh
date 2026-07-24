#!/bin/sh
set -eu

PROGRAM=hydra-standalone-acceptance
SERVER_PID=
DASHBOARD_PID=
TEMP_ROOT=
TEMP_PARENT=

fail()
{
    printf '%s\n' "$PROGRAM: $1" >&2
    exit 1
}

sha256_file()
{
    selected=$1
    if command -v sha256sum >/dev/null 2>&1; then
        output=$(sha256sum "$selected" 2>/dev/null) ||
            fail "checksum calculation failed"
    elif command -v shasum >/dev/null 2>&1; then
        output=$(shasum -a 256 "$selected" 2>/dev/null) ||
            fail "checksum calculation failed"
    else
        fail "SHA-256 tool is unavailable"
    fi
    digest=${output%% *}
    printf '%s\n' "$digest" |
        LC_ALL=C grep -Eq '^[0-9a-f]{64}$' ||
        fail "checksum calculation failed"
    printf '%s\n' "$digest"
}

wait_for_server_port()
{
    wait_server_pid=$1
    wait_port_file=$2
    wait_log_file=$3
    wait_limit=$4
    wait_interval=$5
    wait_count=0
    while [ ! -s "$wait_port_file" ] &&
        [ "$wait_count" -lt "$wait_limit" ] &&
        kill -0 "$wait_server_pid" 2>/dev/null
    do
        wait_count=$((wait_count + 1))
        sleep "$wait_interval"
    done
    if [ -s "$wait_port_file" ]; then
        return 0
    fi
    if kill -0 "$wait_server_pid" 2>/dev/null; then
        kill -KILL "$wait_server_pid" 2>/dev/null || :
    fi
    wait "$wait_server_pid" 2>/dev/null || :
    sed -n '1,20p' "$wait_log_file" >&2
    return 1
}

cleanup()
{
    if [ -n "$DASHBOARD_PID" ]; then
        kill "$DASHBOARD_PID" 2>/dev/null || :
        wait "$DASHBOARD_PID" 2>/dev/null || :
    fi
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || :
        wait "$SERVER_PID" 2>/dev/null || :
    fi
    if [ -n "$TEMP_ROOT" ] && [ -n "$TEMP_PARENT" ]; then
        case "$TEMP_ROOT" in
            "$TEMP_PARENT"/hydra-standalone-accept.*)
                rm -rf "$TEMP_ROOT"
                ;;
        esac
    fi
}
trap cleanup 0 HUP INT TERM

[ "$#" -eq 1 ] || fail "usage: accept_standalone.sh ARCHIVE"
ARCHIVE=$1
[ -f "$ARCHIVE" ] || fail "archive is unavailable"
case "$ARCHIVE" in
    /*) ;;
    *) ARCHIVE=$(CDPATH='' cd -- "$(dirname -- "$ARCHIVE")" && pwd -P)/$(basename -- "$ARCHIVE") ;;
esac

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
DEFAULT_SOURCE_ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd -P)
SOURCE_ROOT=${HYDRA_ACCEPTANCE_SOURCE_ROOT-"$DEFAULT_SOURCE_ROOT"}
HOST_PYTHON=${HYDRA_ACCEPTANCE_PYTHON-}
if [ -z "$HOST_PYTHON" ]; then
    HOST_PYTHON=$(command -v python3.12 2>/dev/null || command -v python3 2>/dev/null || :)
fi
[ -n "$HOST_PYTHON" ] && [ -x "$HOST_PYTHON" ] ||
    fail "host build Python is unavailable"
"$HOST_PYTHON" -c \
    'import PyInstaller; assert PyInstaller.__version__ == "6.21.0"' ||
    fail "PyInstaller 6.21.0 is unavailable"

ARCHIVE_ID=$(
    "$HOST_PYTHON" - "$SOURCE_ROOT/src" "$ARCHIVE" <<'PY'
from pathlib import Path
import re
import sys

sys.path.insert(0, sys.argv[1])
from hydra_codex.archive_validation import validate_tar_members

archive = Path(sys.argv[2])
match = re.fullmatch(
    r"hydra-codex-((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"
    r"-(darwin-arm64|darwin-x86_64|linux-x86_64)\.tar\.gz",
    archive.name,
)
if match is None:
    raise SystemExit(1)
version, target = match.groups()
validated = validate_tar_members(
    archive,
    expected_top_level=f"hydra-codex-{version}",
)
if validated.version != version or validated.target != target:
    raise SystemExit(1)
print(version, target)
PY
) || fail "first archive identity is invalid"
# shellcheck disable=SC2086 # ARCHIVE_ID is exactly the two validated fields above.
set -- $ARCHIVE_ID
[ "$#" -eq 2 ] || fail "first archive identity is invalid"
BASE_VERSION=$1
TARGET=$2
NEXT_VERSION=$(
    "$HOST_PYTHON" - "$BASE_VERSION" <<'PY'
import re
import sys

match = re.fullmatch(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
    sys.argv[1],
)
if match is None:
    raise SystemExit(1)
major, minor, patch = (int(value) for value in match.groups())
print(f"{major}.{minor}.{patch + 1}")
PY
) || fail "next release version is invalid"
case "$(uname -s)/$(uname -m)" in
    Darwin/arm64) NATIVE_TARGET=darwin-arm64 ;;
    Darwin/x86_64) NATIVE_TARGET=darwin-x86_64 ;;
    Linux/x86_64|Linux/amd64) NATIVE_TARGET=linux-x86_64 ;;
    *) fail "unsupported acceptance platform" ;;
esac
[ "$TARGET" = "$NATIVE_TARGET" ] || fail "archive is not native"

TEMP_PARENT=$(CDPATH='' cd -- "${TMPDIR-/tmp}" && pwd -P) ||
    fail "temporary directory is unavailable"
TEMP_ROOT=$(mktemp -d "$TEMP_PARENT/hydra-standalone-accept.XXXXXXXX") ||
    fail "temporary directory creation failed"
chmod 700 "$TEMP_ROOT"
HOME=$TEMP_ROOT/home
export HOME
mkdir -m 700 "$HOME"
CODEX_HOME=$HOME/.codex
export CODEX_HOME
mkdir -m 700 "$CODEX_HOME"
case "$(uname -s)" in
    Darwin) DATA_DIR=$HOME/Library/Application\ Support/Hydra ;;
    Linux) DATA_DIR=$HOME/.local/share/hydra ;;
    *) fail "unsupported acceptance platform" ;;
esac
PROJECT=$TEMP_ROOT/foreign-project
mkdir "$PROJECT"
git -C "$PROJECT" init -q

SHIM_DIR=$TEMP_ROOT/shims
SHIM_LOG=$TEMP_ROOT/shim-invocations.log
mkdir "$SHIM_DIR"
: > "$SHIM_LOG"
for command in python python3 python3.12 pip uv
do
    shim=$SHIM_DIR/$command
    # shellcheck disable=SC2016 # These literals are the generated shim body.
    printf '%s\n' \
        '#!/bin/sh' \
        'printf "%s\n" "$0 $*" >> "$HYDRA_SHIM_LOG"' \
        'exit 97' > "$shim"
    chmod 700 "$shim"
done

CODEX_STATE=$CODEX_HOME/fake-state
mkdir "$CODEX_STATE"
CODEX_FAIL_REFRESH=$CODEX_STATE/fail-refresh
export CODEX_FAIL_REFRESH
CODEX_FAIL_VERSION=$NEXT_VERSION
export CODEX_FAIL_VERSION
cat_codex=$SHIM_DIR/codex
# shellcheck disable=SC2016 # These literals are the generated Codex shim body.
printf '%s\n' \
    '#!/bin/sh' \
    'set -eu' \
    'state=$CODEX_HOME/fake-state' \
    'source_file=$state/marketplace-source' \
    'plugin_file=$state/plugin-installed' \
    'fail_file=$state/fail-refresh' \
    'version() {' \
    '  [ -f "$source_file" ] || { printf "%s\n" ""; return; }' \
    '  sed -n '\''s/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'\'' "$(cat "$source_file")/plugins/hydra-codex/.codex-plugin/plugin.json" | head -1' \
    '}' \
    'if [ "$#" -eq 1 ] && [ "$1" = --version ]; then printf "%s\n" "codex-cli 0.136.0"; exit 0; fi' \
    'if [ "$#" -eq 3 ] && [ "$1 $2 $3" = "plugin marketplace list" ]; then' \
    '  if [ -f "$source_file" ]; then printf "%-13s%s\n" "MARKETPLACE" "ROOT" "hydra" "$(cat "$source_file")"; else printf "%s\n" "No plugin marketplaces in scope."; fi; exit 0' \
    'fi' \
    'if [ "$#" -eq 4 ] && [ "$1 $2 $3" = "plugin marketplace add" ]; then printf "%s\n" "$4" > "$source_file"; printf "%s\n%s\n" "Added marketplace \`hydra\` from $4." "Installed marketplace root: $4"; exit 0; fi' \
    'if [ "$#" -eq 4 ] && [ "$1 $2 $3 $4" = "plugin marketplace remove hydra" ]; then rm -f "$source_file"; printf "%s\n" "Removed marketplace \`hydra\`."; exit 0; fi' \
    'if [ "$#" -eq 4 ] && [ "$1 $2 $3 $4" = "plugin list --marketplace hydra" ]; then' \
    '  if [ ! -f "$source_file" ]; then printf "%s\n" "No plugins found in marketplace \`hydra\`."; exit 0; fi' \
    '  root=$(cat "$source_file"); printf "%s\n%s\n\n" "Marketplace \`hydra\`" "$root/.agents/plugins/marketplace.json"' \
    '  printf "%-22s%-22s%-11s%s\n" "PLUGIN" "STATUS" "VERSION" "PATH"' \
    '  if [ -f "$plugin_file" ]; then installed=$(cat "$plugin_file"); printf "%-22s%-22s%-11s%s\n" "hydra-codex@hydra" "installed, enabled" "$installed" "$root/plugins/hydra-codex"; else printf "%-22s%-22s%-11s%s\n" "hydra-codex@hydra" "not installed" "" "$root/plugins/hydra-codex"; fi; exit 0' \
    'fi' \
    'if [ "$#" -eq 3 ] && [ "$1 $2 $3" = "plugin add hydra-codex@hydra" ]; then current=$(version); if [ -f "$fail_file" ] && [ "$current" = "$CODEX_FAIL_VERSION" ]; then exit 42; fi; printf "%s\n" "$current" > "$plugin_file"; root=$(cat "$source_file"); printf "%s\n%s\n" "Added plugin \`hydra-codex\` from marketplace \`hydra\`." "Installed plugin root: $root/plugins/hydra-codex"; exit 0; fi' \
    'if [ "$#" -eq 3 ] && [ "$1 $2 $3" = "plugin remove hydra-codex@hydra" ]; then rm -f "$plugin_file"; printf "%s\n" "Removed plugin \`hydra-codex\` from marketplace \`hydra\`."; exit 0; fi' \
    'exit 64' > "$cat_codex"
chmod 700 "$cat_codex"

HYDRA_SHIM_LOG=$SHIM_LOG
export HYDRA_SHIM_LOG
PATH=$SHIM_DIR:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset PYTHONPATH PYTHONHOME

SECOND_SOURCE=$TEMP_ROOT/source-$NEXT_VERSION
git clone -q "$SOURCE_ROOT" "$SECOND_SOURCE"
"$HOST_PYTHON" - "$BASE_VERSION" "$NEXT_VERSION" "$SECOND_SOURCE" <<'PY'
from pathlib import Path
import sys

base, next_version, source_value = sys.argv[1:]
source = Path(source_value)
replacements = (
    (
        source / "src" / "hydra_codex" / "__init__.py",
        f'__version__ = "{base}"',
        f'__version__ = "{next_version}"',
    ),
    (
        source / "plugins" / "hydra-codex" / ".codex-plugin" / "plugin.json",
        f'"version": "{base}"',
        f'"version": "{next_version}"',
    ),
)
for path, old, new in replacements:
    content = path.read_text(encoding="utf-8")
    if content.count(old) != 1:
        raise SystemExit(1)
    path.write_text(content.replace(old, new), encoding="utf-8")
PY
git -C "$SECOND_SOURCE" add \
    src/hydra_codex/__init__.py \
    plugins/hydra-codex/.codex-plugin/plugin.json
git -C "$SECOND_SOURCE" \
    -c user.name=Hydra -c user.email=hydra@example.invalid \
    commit -qm "build: create acceptance next release"
SECOND_OUTPUT=$TEMP_ROOT/release-$NEXT_VERSION
"$HOST_PYTHON" "$SECOND_SOURCE/packaging/build_standalone.py" \
    --source-root "$SECOND_SOURCE" --output "$SECOND_OUTPUT" >/dev/null
SECOND_ARCHIVE=$(find "$SECOND_OUTPUT" -type f \
    -name "hydra-codex-$NEXT_VERSION-$TARGET.tar.gz" -print)
[ -n "$SECOND_ARCHIVE" ] && [ "$(printf '%s\n' "$SECOND_ARCHIVE" | wc -l | tr -d ' ')" = 1 ] ||
    fail "second release build failed"

SERVER_ROOT=$TEMP_ROOT/server
mkdir -p "$SERVER_ROOT/releases/download/v$BASE_VERSION" \
    "$SERVER_ROOT/releases/download/v$NEXT_VERSION"
cp "$ARCHIVE" "$SERVER_ROOT/releases/download/v$BASE_VERSION/"
cp "$SECOND_ARCHIVE" "$SERVER_ROOT/releases/download/v$NEXT_VERSION/"
printf '%s\n' "$BASE_VERSION" > "$SERVER_ROOT/latest-version"

write_manifest()
{
    version=$1
    selected=$2
    digest=$(sha256_file "$selected")
    manifest=$SERVER_ROOT/releases/download/v$version/SHA256SUMS
    printf '%s  %s\n' \
        "$digest" "hydra-codex-$version-darwin-arm64.tar.gz" \
        "$digest" "hydra-codex-$version-darwin-x86_64.tar.gz" \
        "$digest" "hydra-codex-$version-linux-x86_64.tar.gz" > "$manifest"
}
write_manifest "$BASE_VERSION" "$ARCHIVE"
write_manifest "$NEXT_VERSION" "$SECOND_ARCHIVE"

SERVER_PORT_FILE=$TEMP_ROOT/server-port
"$HOST_PYTHON" -c '
import http.server
import os
from pathlib import Path
import socketserver
import sys

root = Path(sys.argv[1])
os.chdir(root)


class LoopbackServer(http.server.ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer resolves an FQDN after binding.  A release fixture only
        # needs its numeric loopback address, and hosted macOS DNS can stall.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/releases/latest":
            version = (root / "latest-version").read_text(encoding="utf-8").strip()
            self.send_response(302)
            self.send_header("Location", f"/releases/tag/v{version}")
            self.end_headers()
            return
        if self.path.startswith("/releases/tag/v"):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, _format, *_arguments):
        return

server = LoopbackServer(("127.0.0.1", 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
' "$SERVER_ROOT" > "$SERVER_PORT_FILE" 2>"$TEMP_ROOT/server.log" &
SERVER_PID=$!
if ! wait_for_server_port \
    "$SERVER_PID" "$SERVER_PORT_FILE" "$TEMP_ROOT/server.log" 100 0.05
then
    SERVER_PID=
    fail "release server did not start"
fi
SERVER_PORT=$(sed -n '1p' "$SERVER_PORT_FILE")
RELEASE_BASE=http://127.0.0.1:$SERVER_PORT/releases
HYDRA_INSTALLER_RELEASE_BASE_URL=$RELEASE_BASE
export HYDRA_INSTALLER_RELEASE_BASE_URL

mkdir -p "$HOME/.local/bin"
chmod 755 "$HOME/.local" "$HOME/.local/bin"
sh "$SOURCE_ROOT/install.sh" --version "$BASE_VERSION" >/dev/null
HYDRA=$HOME/.local/bin/hydra-codex
[ -x "$HYDRA" ] && [ ! -L "$HOME/.hydra/current/bin/hydra-codex" ] ||
    fail "installed launcher is unavailable"
[ "$("$HYDRA" --version)" = "hydra-codex $BASE_VERSION" ] ||
    fail "installed version smoke failed"
"$HYDRA" install -y >/dev/null
"$HYDRA" init "$PROJECT" >/dev/null
"$HYDRA" status "$PROJECT" --json > "$TEMP_ROOT/status.json"
grep -q '"initialized":true' "$TEMP_ROOT/status.json" ||
    fail "installed status failed"

SESSION_DIR=$CODEX_HOME/sessions/2026/07/23
mkdir -p "$SESSION_DIR"
SESSION_FILE=$SESSION_DIR/rollout-2026-07-23T00-00-00.000Z-acceptance.jsonl
printf '{"timestamp":"2026-07-23T00:00:00Z","type":"session_meta","payload":{"id":"acceptance","session_id":"acceptance","cwd":"%s"}}\n' \
    "$PROJECT" > "$SESSION_FILE"
"$HYDRA" ingest --cwd "$PROJECT" > "$TEMP_ROOT/ingest.json"
grep -q '"files_seen":1' "$TEMP_ROOT/ingest.json" ||
    fail "synthetic ingest failed"
"$HYDRA" reconcile --cwd "$PROJECT" > "$TEMP_ROOT/reconcile.json"
grep -q '"status":"ok"' "$TEMP_ROOT/reconcile.json" ||
    fail "synthetic reconcile failed"
"$HYDRA" report --last 1 --format json --cwd "$PROJECT" \
    > "$TEMP_ROOT/report.json"
"$HOST_PYTHON" - "$TEMP_ROOT/report.json" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("schema_version") != "hydra.report-list/v1":
    raise SystemExit(1)
reports = payload.get("reports")
if not isinstance(reports, list) or len(reports) != 1:
    raise SystemExit(1)
report = reports[0]
if (
    not isinstance(report, dict)
    or report.get("schema_version") != "hydra.report/v3"
    or not isinstance(report.get("task_ref"), str)
    or not report["task_ref"].startswith("task_")
):
    raise SystemExit(1)
PY

printf '{"hook_event_name":"UserPromptSubmit","session_id":"acceptance","turn_id":"turn-a","cwd":"%s","prompt":"acceptance"}\n' \
    "$PROJECT" | "$HYDRA" hook > "$TEMP_ROOT/hook.json"
grep -q '"hookSpecificOutput"' "$TEMP_ROOT/hook.json" ||
    fail "frozen hook failed"
printf '%s\n%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' |
    "$HYDRA" mcp > "$TEMP_ROOT/mcp.jsonl"
grep -q '"serverInfo"' "$TEMP_ROOT/mcp.jsonl" ||
    fail "frozen MCP failed"
grep -q '"tools"' "$TEMP_ROOT/mcp.jsonl" ||
    fail "frozen MCP failed"

DASHBOARD_URL_FILE=$TEMP_ROOT/dashboard-url
"$HYDRA" dashboard --no-open --port 0 --cwd "$PROJECT" \
    > "$DASHBOARD_URL_FILE" 2>"$TEMP_ROOT/dashboard.log" &
DASHBOARD_PID=$!
count=0
while [ ! -s "$DASHBOARD_URL_FILE" ] && [ "$count" -lt 200 ]
do
    count=$((count + 1))
    sleep 0.05
done
[ -s "$DASHBOARD_URL_FILE" ] || fail "dashboard did not start"
DASHBOARD_URL=$(sed -n '1p' "$DASHBOARD_URL_FILE")
DASHBOARD_BASE=${DASHBOARD_URL%%#*}
DASHBOARD_TOKEN=${DASHBOARD_URL#*#token=}
curl -fsS "${DASHBOARD_BASE%/}/assets/views/evidence.js" \
    > "$TEMP_ROOT/evidence.js"
[ -s "$TEMP_ROOT/evidence.js" ] || fail "dashboard static asset failed"
curl -fsS -H "Authorization: Bearer $DASHBOARD_TOKEN" \
    "${DASHBOARD_BASE%/}/api/v1/snapshot" > "$TEMP_ROOT/snapshot.json"
grep -q '"schema_version"' "$TEMP_ROOT/snapshot.json" ||
    fail "dashboard health failed"
kill "$DASHBOARD_PID"
wait "$DASHBOARD_PID" 2>/dev/null || :
DASHBOARD_PID=

printf '%s\n' "$NEXT_VERSION" > "$SERVER_ROOT/latest-version"
"$HYDRA" upgrade --check > "$TEMP_ROOT/runtime-check.json"
"$HOST_PYTHON" - "$TEMP_ROOT/runtime-check.json" \
    "$BASE_VERSION" "$NEXT_VERSION" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = {
    "command": "upgrade",
    "current_version": sys.argv[2],
    "latest_version": sys.argv[3],
    "status": "ok",
    "update_available": True,
}
if payload != expected:
    raise SystemExit(1)
PY

: > "$CODEX_FAIL_REFRESH"
if "$HYDRA" upgrade >/dev/null 2>&1; then
    fail "forced Codex refresh unexpectedly succeeded"
fi
[ "$("$HYDRA" --version)" = "hydra-codex $BASE_VERSION" ] ||
    fail "forced Codex refresh rollback failed"
grep -q "/versions/$BASE_VERSION/marketplace" "$CODEX_STATE/marketplace-source" ||
    fail "forced Codex refresh rollback failed"
grep -q "\"runtime_version\":\"$BASE_VERSION\"" \
        "$DATA_DIR/codex-integration.json" ||
    fail "forced Codex refresh rollback failed"
rm -f "$CODEX_FAIL_REFRESH"
"$HYDRA" upgrade >/dev/null
[ "$("$HYDRA" --version)" = "hydra-codex $NEXT_VERSION" ] ||
    fail "runtime upgrade did not activate"
grep -q "\"runtime_version\":\"$NEXT_VERSION\"" \
    "$DATA_DIR/codex-integration.json" ||
    fail "Codex refresh did not commit"

DATABASE=$DATA_DIR/hydra.sqlite3
[ -f "$DATABASE" ] || fail "telemetry database is unavailable"
DATABASE_DIGEST=$(sha256_file "$DATABASE")
"$HYDRA" uninstall -y >/dev/null
[ ! -e "$HYDRA" ] && [ ! -e "$HOME/.hydra/current" ] ||
    fail "full uninstall retained CLI state"
[ -f "$DATABASE" ] || fail "uninstall removed telemetry"
[ "$(sha256_file "$DATABASE")" = "$DATABASE_DIGEST" ] ||
    fail "uninstall changed telemetry"

if [ -s "$SHIM_LOG" ]; then
    fail "shim invocation log is not empty"
fi
printf '%s\n' "standalone acceptance passed"
