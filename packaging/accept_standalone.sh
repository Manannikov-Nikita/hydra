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
    *) ARCHIVE=$(CDPATH= cd -- "$(dirname -- "$ARCHIVE")" && pwd -P)/$(basename -- "$ARCHIVE") ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
DEFAULT_SOURCE_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
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

TEMP_PARENT=$(CDPATH= cd -- "${TMPDIR-/tmp}" && pwd -P) ||
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
cat_codex=$SHIM_DIR/codex
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
    'if [ "$#" -eq 1 ] && [ "$1" = --version ]; then printf "%s\n" "codex-cli 1.0.0"; exit 0; fi' \
    'if [ "$1 $2 $3" = "plugin marketplace list" ]; then' \
    '  if [ -f "$source_file" ]; then printf '\''[{"name":"hydra","source":"%s"}]\n'\'' "$(cat "$source_file")"; else printf "%s\n" "[]"; fi; exit 0' \
    'fi' \
    'if [ "$1 $2 $3" = "plugin marketplace add" ]; then printf "%s\n" "$4" > "$source_file"; printf "%s\n" "{}"; exit 0; fi' \
    'if [ "$1 $2 $3" = "plugin marketplace remove" ]; then rm -f "$source_file"; printf "%s\n" "{}"; exit 0; fi' \
    'if [ "$1 $2" = "plugin list" ]; then' \
    '  if [ -f "$source_file" ]; then current=$(version); installed=false; [ -f "$plugin_file" ] && installed=true; printf '\''[{"name":"hydra-codex","marketplace":"hydra","installed":%s,"version":"%s"}]\n'\'' "$installed" "$current"; else printf "%s\n" "[]"; fi; exit 0' \
    'fi' \
    'if [ "$1 $2" = "plugin add" ]; then current=$(version); if [ -f "$fail_file" ] && [ "$current" = 0.1.1 ]; then exit 42; fi; printf "%s\n" "$current" > "$plugin_file"; printf "%s\n" "{}"; exit 0; fi' \
    'if [ "$1 $2" = "plugin remove" ]; then rm -f "$plugin_file"; printf "%s\n" "{}"; exit 0; fi' \
    'exit 64' > "$cat_codex"
chmod 700 "$cat_codex"

HYDRA_SHIM_LOG=$SHIM_LOG
export HYDRA_SHIM_LOG
PATH=$SHIM_DIR:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset PYTHONPATH PYTHONHOME

SECOND_SOURCE=$TEMP_ROOT/source-0.1.1
git clone -q "$SOURCE_ROOT" "$SECOND_SOURCE"
sed 's/__version__ = "0.1.0"/__version__ = "0.1.1"/' \
    "$SECOND_SOURCE/src/hydra_codex/__init__.py" > "$SECOND_SOURCE/version.tmp"
mv "$SECOND_SOURCE/version.tmp" "$SECOND_SOURCE/src/hydra_codex/__init__.py"
sed 's/"version": "0.1.0"/"version": "0.1.1"/' \
    "$SECOND_SOURCE/plugins/hydra-codex/.codex-plugin/plugin.json" \
    > "$SECOND_SOURCE/plugin.tmp"
mv "$SECOND_SOURCE/plugin.tmp" \
    "$SECOND_SOURCE/plugins/hydra-codex/.codex-plugin/plugin.json"
grep -q '__version__ = "0.1.1"' "$SECOND_SOURCE/src/hydra_codex/__init__.py" ||
    fail "second source version update failed"
grep -q '"version": "0.1.1"' \
    "$SECOND_SOURCE/plugins/hydra-codex/.codex-plugin/plugin.json" ||
    fail "second plugin version update failed"
git -C "$SECOND_SOURCE" add \
    src/hydra_codex/__init__.py \
    plugins/hydra-codex/.codex-plugin/plugin.json
git -C "$SECOND_SOURCE" \
    -c user.name=Hydra -c user.email=hydra@example.invalid \
    commit -qm "build: create acceptance release 0.1.1"
SECOND_OUTPUT=$TEMP_ROOT/release-0.1.1
"$HOST_PYTHON" "$SECOND_SOURCE/packaging/build_standalone.py" \
    --source-root "$SECOND_SOURCE" --output "$SECOND_OUTPUT" >/dev/null
SECOND_ARCHIVE=$(find "$SECOND_OUTPUT" -type f \
    -name 'hydra-codex-0.1.1-*.tar.gz' -print)
[ -n "$SECOND_ARCHIVE" ] && [ "$(printf '%s\n' "$SECOND_ARCHIVE" | wc -l | tr -d ' ')" = 1 ] ||
    fail "second release build failed"

case "$(basename "$ARCHIVE")" in
    hydra-codex-0.1.0-darwin-arm64.tar.gz) TARGET=darwin-arm64 ;;
    hydra-codex-0.1.0-darwin-x86_64.tar.gz) TARGET=darwin-x86_64 ;;
    hydra-codex-0.1.0-linux-x86_64.tar.gz) TARGET=linux-x86_64 ;;
    *) fail "first archive identity is invalid" ;;
esac
case "$(uname -s)/$(uname -m)" in
    Darwin/arm64) NATIVE_TARGET=darwin-arm64 ;;
    Darwin/x86_64) NATIVE_TARGET=darwin-x86_64 ;;
    Linux/x86_64|Linux/amd64) NATIVE_TARGET=linux-x86_64 ;;
    *) fail "unsupported acceptance platform" ;;
esac
[ "$TARGET" = "$NATIVE_TARGET" ] || fail "archive is not native"

SERVER_ROOT=$TEMP_ROOT/server
mkdir -p "$SERVER_ROOT/releases/download/v0.1.0" \
    "$SERVER_ROOT/releases/download/v0.1.1"
cp "$ARCHIVE" "$SERVER_ROOT/releases/download/v0.1.0/"
cp "$SECOND_ARCHIVE" "$SERVER_ROOT/releases/download/v0.1.1/"
printf '%s\n' "0.1.0" > "$SERVER_ROOT/latest-version"

write_manifest()
{
    version=$1
    selected=$2
    digest=$(shasum -a 256 "$selected" | awk '{print $1}')
    manifest=$SERVER_ROOT/releases/download/v$version/SHA256SUMS
    printf '%s  %s\n' \
        "$digest" "hydra-codex-$version-darwin-arm64.tar.gz" \
        "$digest" "hydra-codex-$version-darwin-x86_64.tar.gz" \
        "$digest" "hydra-codex-$version-linux-x86_64.tar.gz" > "$manifest"
}
write_manifest 0.1.0 "$ARCHIVE"
write_manifest 0.1.1 "$SECOND_ARCHIVE"

SERVER_PORT_FILE=$TEMP_ROOT/server-port
"$HOST_PYTHON" -c '
import http.server
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
os.chdir(root)

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

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
' "$SERVER_ROOT" > "$SERVER_PORT_FILE" 2>"$TEMP_ROOT/server.log" &
SERVER_PID=$!
count=0
while [ ! -s "$SERVER_PORT_FILE" ] && [ "$count" -lt 100 ]
do
    count=$((count + 1))
    sleep 0.05
done
[ -s "$SERVER_PORT_FILE" ] || fail "release server did not start"
SERVER_PORT=$(sed -n '1p' "$SERVER_PORT_FILE")
RELEASE_BASE=http://127.0.0.1:$SERVER_PORT/releases
export HYDRA_INSTALLER_RELEASE_BASE_URL=$RELEASE_BASE

sh "$SOURCE_ROOT/install.sh" --version 0.1.0 >/dev/null
HYDRA=$HOME/.local/bin/hydra-codex
[ -x "$HYDRA" ] && [ ! -L "$HOME/.hydra/current/bin/hydra-codex" ] ||
    fail "installed launcher is unavailable"
[ "$("$HYDRA" --version)" = "hydra-codex 0.1.0" ] ||
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

printf '{"hook_event_name":"UserPromptSubmit","session_id":"acceptance","turn_id":"turn-a","cwd":"%s","prompt":"acceptance"}\n' \
    "$PROJECT" | "$HYDRA" hook > "$TEMP_ROOT/hook.json"
grep -q '"hookSpecificOutput"' "$TEMP_ROOT/hook.json" ||
    fail "frozen hook failed"
printf '%s\n%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' |
    "$HYDRA" mcp > "$TEMP_ROOT/mcp.jsonl"
grep -q '"serverInfo"' "$TEMP_ROOT/mcp.jsonl" &&
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

printf '%s\n' "0.1.1" > "$SERVER_ROOT/latest-version"
sh "$SOURCE_ROOT/install.sh" --check > "$TEMP_ROOT/upgrade-check.txt"
grep -q 'Hydra update available: 0.1.0 -> 0.1.1' \
    "$TEMP_ROOT/upgrade-check.txt" || fail "upgrade --check failed"
"$HYDRA" upgrade --check > "$TEMP_ROOT/runtime-check.json"
sh "$SOURCE_ROOT/install.sh" --version 0.1.1 >/dev/null
[ "$("$HYDRA" --version)" = "hydra-codex 0.1.1" ] ||
    fail "explicit release switch failed"

: > "$CODEX_FAIL_REFRESH"
if "$HYDRA" install -y --refresh >/dev/null 2>&1; then
    fail "forced Codex refresh unexpectedly succeeded"
fi
grep -q '/versions/0.1.0/marketplace' "$CODEX_STATE/marketplace-source" &&
    grep -q '"runtime_version":"0.1.0"' \
        "$DATA_DIR/codex-integration.json" ||
    fail "forced Codex refresh rollback failed"
rm -f "$CODEX_FAIL_REFRESH"
"$HYDRA" install -y --refresh >/dev/null
grep -q '"runtime_version":"0.1.1"' \
    "$DATA_DIR/codex-integration.json" ||
    fail "Codex refresh did not commit"

DATABASE=$DATA_DIR/hydra.sqlite3
[ -f "$DATABASE" ] || fail "telemetry database is unavailable"
DATABASE_DIGEST=$(shasum -a 256 "$DATABASE" | awk '{print $1}')
"$HYDRA" uninstall -y >/dev/null
[ ! -e "$HYDRA" ] && [ ! -e "$HOME/.hydra/current" ] ||
    fail "full uninstall retained CLI state"
[ -f "$DATABASE" ] || fail "uninstall removed telemetry"
[ "$(shasum -a 256 "$DATABASE" | awk '{print $1}')" = "$DATABASE_DIGEST" ] ||
    fail "uninstall changed telemetry"

if [ -s "$SHIM_LOG" ]; then
    fail "shim invocation log is not empty"
fi
printf '%s\n' "standalone acceptance passed"
