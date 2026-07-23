#!/bin/sh
set -eu

umask 077

PROGRAM=hydra-installer
PRODUCTION_RELEASE_BASE=https://github.com/Manannikov-Nikita/hydra/releases
DOWNLOAD_DIR=
STAGE_DIR=
ACQUISITION_CAPABILITY=
INSTALLED_VERSION=
BUNDLE_TARGET=

fail()
{
    printf '%s\n' "$PROGRAM: $1" >&2
    exit 1
}

path_exists()
{
    [ -e "$1" ] || [ -L "$1" ]
}

cleanup()
{
    if [ -n "$STAGE_DIR" ]; then
        case "$STAGE_DIR" in
            "$HOME/.hydra"/.staging.*|"$HOME/.hydra"/.acquire.*)
                rm -rf "$STAGE_DIR"
                ;;
        esac
    fi
    if [ -n "$DOWNLOAD_DIR" ]; then
        case "$DOWNLOAD_DIR" in
            "$HOME"/.hydra-download.*) rm -rf "$DOWNLOAD_DIR" ;;
        esac
    fi
    :
}
trap cleanup 0 HUP INT TERM

validate_home()
{
    [ -n "${HOME-}" ] || fail "HOME is unavailable"
    case "$HOME" in
        /*) ;;
        *) fail "HOME is invalid" ;;
    esac
    [ "$(printf '%s\n' "$HOME" | wc -l | tr -d ' ')" = 1 ] ||
        fail "HOME is invalid"
    [ -d "$HOME" ] && [ ! -L "$HOME" ] || fail "HOME is invalid"
    EFFECTIVE_UID=$(id -u 2>/dev/null) || fail "user identity is unavailable"
    case "$EFFECTIVE_UID" in
        ''|*[!0-9]*) fail "user identity is unavailable" ;;
    esac
    home_record=$(LC_ALL=C ls -ldn "$HOME" 2>/dev/null) ||
        fail "HOME is invalid"
    home_mode=$(printf '%s\n' "$home_record" | awk '{print $1}')
    home_uid=$(printf '%s\n' "$home_record" | awk '{print $3}')
    [ "$home_uid" = "$EFFECTIVE_UID" ] || fail "HOME ownership is unsafe"
    case "$home_mode" in
        ?????w*|????????w*) fail "HOME permissions are unsafe" ;;
    esac
}

validate_acquisition_capability()
{
    capability_file=$HOME/.hydra-installer-capability.$1
    [ -f "$capability_file" ] && [ ! -L "$capability_file" ] ||
        fail "another installation is in progress"
    capability_record=$(LC_ALL=C ls -ldn "$capability_file" 2>/dev/null) ||
        fail "another installation is in progress"
    capability_mode=$(printf '%s\n' "$capability_record" | awk '{print $1}')
    capability_uid=$(printf '%s\n' "$capability_record" | awk '{print $3}')
    [ "$capability_uid" = "$EFFECTIVE_UID" ] ||
        fail "another installation is in progress"
    case "$capability_mode" in
        -rw-------*) ;;
        *) fail "another installation is in progress" ;;
    esac
    capability_bytes=$(
        { wc -c < "$capability_file"; } 2>/dev/null | tr -d ' '
    ) || fail "another installation is in progress"
    [ "$capability_bytes" = 30 ] ||
        fail "another installation is in progress"
    capability_lines=$(
        { wc -l < "$capability_file"; } 2>/dev/null | tr -d ' '
    ) || fail "another installation is in progress"
    [ "$capability_lines" = 1 ] ||
        fail "another installation is in progress"
    capability_value=$(sed -n '1p' "$capability_file" 2>/dev/null) ||
        fail "another installation is in progress"
    [ "$capability_value" = hydra-installer-capability/v1 ] ||
        fail "another installation is in progress"
}

valid_version()
{
    value=$1
    [ "${#value}" -le 64 ] || return 1
    printf '%s\n' "$value" |
        LC_ALL=C grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
}

detect_target()
{
    system=$(uname -s 2>/dev/null) || fail "platform detection failed"
    machine=$(uname -m 2>/dev/null) || fail "platform detection failed"
    case "$system/$machine" in
        Darwin/arm64) TARGET=darwin-arm64 ;;
        Darwin/x86_64) TARGET=darwin-x86_64 ;;
        Linux/x86_64|Linux/amd64) TARGET=linux-x86_64 ;;
        *) fail "unsupported platform" ;;
    esac
}

validate_release_base()
{
    if [ "${HYDRA_INSTALLER_RELEASE_BASE_URL+x}" = x ]; then
        RELEASE_BASE=$HYDRA_INSTALLER_RELEASE_BASE_URL
        printf '%s\n' "$RELEASE_BASE" |
            LC_ALL=C grep -Eq \
                '^http://(127\.0\.0\.1|\[::1\]):[0-9]+/releases$' ||
            fail "release source is invalid"
        case "$RELEASE_BASE" in
            http://127.0.0.1:*)
                port_path=${RELEASE_BASE#http://127.0.0.1:}
                ;;
            http://\[\:\:1\]:*)
                port_path=${RELEASE_BASE#http://\[::1\]:}
                ;;
            *) fail "release source is invalid" ;;
        esac
        port=${port_path%/releases}
        [ "$port" -ge 1 ] 2>/dev/null && [ "$port" -le 65535 ] 2>/dev/null ||
            fail "release source is invalid"
        RELEASE_ORIGIN=${RELEASE_BASE%/releases}
        CURL_PROTOCOL=http
        LOCAL_RELEASE_SOURCE=1
    else
        RELEASE_BASE=$PRODUCTION_RELEASE_BASE
        RELEASE_ORIGIN=https://github.com
        CURL_PROTOCOL=https
        LOCAL_RELEASE_SOURCE=0
    fi
}

curl_to_file()
{
    url=$1
    destination=$2
    expected=$3
    maximum_bytes=$4
    effective=$(curl -sS -L --fail --connect-timeout 10 --max-time 120 \
        --max-filesize "$maximum_bytes" \
        --proto "=$CURL_PROTOCOL" --proto-redir "=$CURL_PROTOCOL" \
        -o "$destination" -w '%{url_effective}' "$url" 2>/dev/null) ||
        fail "download failed"
    downloaded_bytes=$(
        { wc -c < "$destination"; } 2>/dev/null | tr -d ' '
    ) || fail "download validation failed"
    case "$downloaded_bytes" in
        ''|*[!0-9]*) fail "download validation failed" ;;
    esac
    [ "$downloaded_bytes" -le "$maximum_bytes" ] 2>/dev/null ||
        fail "download exceeds size limit"
    if [ "$LOCAL_RELEASE_SOURCE" = 1 ]; then
        [ "$effective" = "$expected" ] ||
            fail "download redirect was rejected"
        return
    fi
    if [ "$effective" = "$expected" ]; then
        return
    fi
    case "$effective" in
        https://release-assets.githubusercontent.com/github-production-release-asset/*)
            case "$effective" in
                *'#'*|*'
'*) fail "download redirect was rejected" ;;
            esac
            ;;
        *) fail "download redirect was rejected" ;;
    esac
}

resolve_latest()
{
    effective=$(curl -sS -L --fail --connect-timeout 10 --max-time 30 \
        --proto "=$CURL_PROTOCOL" --proto-redir "=$CURL_PROTOCOL" \
        -o /dev/null -w '%{url_effective}' "$RELEASE_BASE/latest" 2>/dev/null) ||
        fail "latest release lookup failed"
    if [ "$LOCAL_RELEASE_SOURCE" = 1 ]; then
        prefix="$RELEASE_ORIGIN/releases/tag/v"
    else
        prefix="$RELEASE_ORIGIN/Manannikov-Nikita/hydra/releases/tag/v"
    fi
    case "$effective" in
        "$prefix"*) version=${effective#"$prefix"} ;;
        *) fail "latest release redirect was rejected" ;;
    esac
    valid_version "$version" || fail "latest release is invalid"
    [ "$effective" = "$prefix$version" ] ||
        fail "latest release redirect was rejected"
    VERSION=$version
}

validate_public_directory()
{
    directory=$1
    [ -d "$directory" ] && [ ! -L "$directory" ] ||
        fail "local installation ownership is invalid"
    directory_record=$(LC_ALL=C ls -ldn "$directory" 2>/dev/null) ||
        fail "local installation ownership is invalid"
    directory_mode=$(printf '%s\n' "$directory_record" | awk '{print $1}')
    directory_uid=$(printf '%s\n' "$directory_record" | awk '{print $3}')
    [ "$directory_uid" = "$EFFECTIVE_UID" ] ||
        fail "local installation ownership is invalid"
    case "$directory_mode" in
        ?????w*|????????w*)
            fail "local installation ownership is invalid"
            ;;
    esac
}

safe_public_directory()
{
    directory=$1
    if path_exists "$directory"; then
        validate_public_directory "$directory"
    else
        mkdir -m 700 "$directory" 2>/dev/null ||
            fail "installation directory creation failed"
    fi
}

safe_private_directory()
{
    directory=$1
    if path_exists "$directory"; then
        [ -d "$directory" ] && [ ! -L "$directory" ] ||
            fail "local installation ownership is invalid"
        directory_record=$(LC_ALL=C ls -ldn "$directory" 2>/dev/null) ||
            fail "local installation ownership is invalid"
        directory_mode=$(printf '%s\n' "$directory_record" | awk '{print $1}')
        directory_uid=$(printf '%s\n' "$directory_record" | awk '{print $3}')
        [ "$directory_uid" = "$EFFECTIVE_UID" ] ||
            fail "local installation ownership is invalid"
        case "$directory_mode" in
            d???------*) ;;
            *) fail "local installation ownership is invalid" ;;
        esac
    else
        mkdir -m 700 "$directory" 2>/dev/null ||
            fail "installation directory creation failed"
    fi
}

require_regular()
{
    [ -f "$1" ] && [ ! -L "$1" ] ||
        fail "release bundle inventory is invalid"
}

read_exact_marker()
{
    marker=$1
    expected=$2
    require_regular "$marker"
    marker_bytes=$(
        { wc -c < "$marker"; } 2>/dev/null | tr -d ' '
    ) || fail "release bundle metadata is invalid"
    [ "$marker_bytes" -le 256 ] 2>/dev/null ||
        fail "release bundle metadata is invalid"
    marker_lines=$(
        { wc -l < "$marker"; } 2>/dev/null | tr -d ' '
    ) || fail "release bundle metadata is invalid"
    [ "$marker_lines" = 1 ] ||
        fail "release bundle metadata is invalid"
    marker_value=$(sed -n '1p' "$marker" 2>/dev/null) ||
        fail "release bundle metadata is invalid"
    [ "$marker_value" = "$expected" ] ||
        fail "release bundle metadata does not match"
}

validate_bundle_directory()
{
    bundle=$1
    expected_version=$2
    expected_target=${3-}
    [ -d "$bundle" ] && [ ! -L "$bundle" ] ||
        fail "release bundle is invalid"

    for entry in "$bundle"/* "$bundle"/.[!.]* "$bundle"/..?*; do
        path_exists "$entry" || continue
        name=${entry##*/}
        case "$name" in
            VERSION|TARGET|LICENSE|install.sh|bin|runtime|marketplace) ;;
            *) fail "release bundle inventory is invalid" ;;
        esac
    done
    read_exact_marker "$bundle/VERSION" "$expected_version"
    require_regular "$bundle/TARGET"
    target_bytes=$(
        { wc -c < "$bundle/TARGET"; } 2>/dev/null | tr -d ' '
    ) || fail "release bundle metadata is invalid"
    [ "$target_bytes" -le 256 ] 2>/dev/null ||
        fail "release bundle metadata is invalid"
    target_lines=$(
        { wc -l < "$bundle/TARGET"; } 2>/dev/null | tr -d ' '
    ) || fail "release bundle metadata is invalid"
    [ "$target_lines" = 1 ] ||
        fail "release bundle metadata is invalid"
    BUNDLE_TARGET=$(sed -n '1p' "$bundle/TARGET" 2>/dev/null) ||
        fail "release bundle metadata is invalid"
    case "$BUNDLE_TARGET" in
        darwin-arm64|darwin-x86_64|linux-x86_64) ;;
        *) fail "release bundle target is invalid" ;;
    esac
    if [ -n "$expected_target" ]; then
        [ "$BUNDLE_TARGET" = "$expected_target" ] ||
            fail "release bundle target does not match"
    fi

    require_regular "$bundle/LICENSE"
    require_regular "$bundle/install.sh"
    [ -d "$bundle/bin" ] && [ ! -L "$bundle/bin" ] ||
        fail "release bundle inventory is invalid"
    require_regular "$bundle/bin/hydra-codex"
    [ -x "$bundle/bin/hydra-codex" ] ||
        fail "release bundle executable is invalid"
    for entry in "$bundle/bin"/* "$bundle/bin"/.[!.]*; do
        path_exists "$entry" || continue
        [ "${entry##*/}" = hydra-codex ] ||
            fail "release bundle inventory is invalid"
    done
    [ -d "$bundle/runtime" ] && [ ! -L "$bundle/runtime" ] ||
        fail "release bundle inventory is invalid"
    [ -d "$bundle/marketplace" ] && [ ! -L "$bundle/marketplace" ] ||
        fail "release bundle inventory is invalid"
    require_regular \
        "$bundle/marketplace/.agents/plugins/marketplace.json"
    plugin="$bundle/marketplace/plugins/hydra-codex"
    [ -d "$plugin" ] && [ ! -L "$plugin" ] ||
        fail "release bundle inventory is invalid"
    for relative in \
        .codex-plugin/plugin.json \
        .mcp.json \
        README.md \
        hooks/hooks.json \
        skills/hydra-report/SKILL.md \
        skills/hydra-report/agents/openai.yaml \
        skills/hydra-report/references/report-schema.md
    do
        require_regular "$plugin/$relative"
    done
}

inspect_local_state()
{
    hydra_root=$HOME/.hydra
    versions=$hydra_root/versions
    current=$hydra_root/current
    launcher=$HOME/.local/bin/hydra-codex
    INSTALLED_VERSION=

    if path_exists "$HOME/.local"; then
        validate_public_directory "$HOME/.local"
        if path_exists "$HOME/.local/bin"; then
            validate_public_directory "$HOME/.local/bin"
        fi
    fi
    if ! path_exists "$hydra_root"; then
        path_exists "$current" &&
            fail "local installation ownership is invalid"
        path_exists "$launcher" &&
            fail "local installation ownership is invalid"
        return 0
    fi
    safe_private_directory "$hydra_root"
    if path_exists "$versions"; then
        safe_private_directory "$versions"
    fi
    if path_exists "$current"; then
        [ -L "$current" ] ||
            fail "local installation ownership is invalid"
        current_target=$(readlink "$current" 2>/dev/null) ||
            fail "local installation ownership is invalid"
        case "$current_target" in
            "$versions"/*) installed=${current_target#"$versions"/} ;;
            *) fail "local installation ownership is invalid" ;;
        esac
        valid_version "$installed" ||
            fail "local installation ownership is invalid"
        [ "$current_target" = "$versions/$installed" ] ||
            fail "local installation ownership is invalid"
        validate_bundle_directory "$current_target" "$installed" ""
        INSTALLED_VERSION=$installed
    fi
    if path_exists "$launcher"; then
        [ -L "$launcher" ] ||
            fail "local installation ownership is invalid"
        launcher_target=$(readlink "$launcher" 2>/dev/null) ||
            fail "local installation ownership is invalid"
        [ "$launcher_target" = "$current/bin/hydra-codex" ] &&
            [ -n "$INSTALLED_VERSION" ] ||
            fail "local installation ownership is invalid"
    elif [ -n "$INSTALLED_VERSION" ]; then
        fail "local installation ownership is invalid"
    fi
    return 0
}

validate_manifest()
{
    manifest=$1
    [ "$(wc -l < "$manifest" | tr -d ' ')" = 3 ] ||
        fail "checksum manifest is invalid"
    LC_ALL=C grep -Eq \
        '^[0-9a-f]{64}  [^[:space:]]+$' "$manifest" ||
        fail "checksum manifest is invalid"
    [ "$(LC_ALL=C grep -Ec '^[0-9a-f]{64}  [^[:space:]]+$' "$manifest")" = 3 ] ||
        fail "checksum manifest is invalid"

    first="hydra-codex-$VERSION-darwin-arm64.tar.gz"
    second="hydra-codex-$VERSION-darwin-x86_64.tar.gz"
    third="hydra-codex-$VERSION-linux-x86_64.tar.gz"
    [ "$(sed -n '1s/^[0-9a-f][0-9a-f]*  //p' "$manifest")" = "$first" ] &&
        [ "$(sed -n '2s/^[0-9a-f][0-9a-f]*  //p' "$manifest")" = "$second" ] &&
        [ "$(sed -n '3s/^[0-9a-f][0-9a-f]*  //p' "$manifest")" = "$third" ] ||
        fail "checksum manifest is invalid"
}

verify_checksum()
{
    archive=$1
    manifest=$2
    case "$TARGET" in
        darwin-arm64) row=1 ;;
        darwin-x86_64) row=2 ;;
        linux-x86_64) row=3 ;;
    esac
    expected=$(sed -n "${row}s/  .*//p" "$manifest")
    if command -v sha256sum >/dev/null 2>&1; then
        output=$(sha256sum "$archive" 2>/dev/null) ||
            fail "checksum verification failed"
    elif command -v shasum >/dev/null 2>&1; then
        output=$(shasum -a 256 "$archive" 2>/dev/null) ||
            fail "checksum verification failed"
    else
        fail "SHA-256 tool is unavailable"
    fi
    actual=${output%% *}
    printf '%s\n' "$actual" |
        LC_ALL=C grep -Eq '^[0-9a-f]{64}$' ||
        fail "checksum verification failed"
    [ "$actual" = "$expected" ] || fail "checksum verification failed"
}

preflight_archive()
{
    archive=$1
    expected_top=$2
    names=$DOWNLOAD_DIR/archive.names
    verbose=$DOWNLOAD_DIR/archive.verbose
    normalized=$DOWNLOAD_DIR/archive.normalized
    tar -tzf "$archive" >"$names" 2>/dev/null ||
        fail "release archive validation failed"
    tar -tvzf "$archive" >"$verbose" 2>/dev/null ||
        fail "release archive validation failed"
    count=$(wc -l < "$names" | tr -d ' ')
    [ "$count" -ge 1 ] 2>/dev/null && [ "$count" -le 4096 ] 2>/dev/null ||
        fail "release archive validation failed"
    LC_ALL=C awk -v top="$expected_top" '
        {
            name=$0
            if (name == "" || substr(name,1,1) == "/" ||
                index(name,"\\") || index(name,"//") ||
                name ~ /[[:cntrl:]]/) exit 1
            if (substr(name,length(name),1) == "/")
                name=substr(name,1,length(name)-1)
            count=split(name,part,"/")
            for (i=1; i<=count; i++)
                if (part[i] == "" || part[i] == "." || part[i] == "..")
                    exit 1
            if (name != top && index(name,top "/") != 1) exit 1
            print name
        }
    ' "$names" >"$normalized" 2>/dev/null ||
        fail "release archive validation failed"
    [ "$(LC_ALL=C sort "$normalized" | uniq -d | wc -l | tr -d ' ')" = 0 ] ||
        fail "release archive validation failed"
    LC_ALL=C awk '
        {
            mode=$1
            if (mode !~ /^[-d][rwx-]{9}/ || mode ~ /[sStT]/) exit 1
            if (substr(mode,1,1) == "-" && substr(mode,2,1) != "r") exit 1
            if (substr(mode,1,1) == "d" &&
                (substr(mode,2,1) != "r" || substr(mode,4,1) != "x")) exit 1
            if ($2 ~ /^[0-9]+$/ && $5 ~ /^[0-9]+$/) size=$5
            else if ($3 ~ /^[0-9]+$/) size=$3
            else exit 1
            if (size > 67108864) exit 1
            total += size
            if (total > 536870912) exit 1
        }
    ' "$verbose" >/dev/null 2>&1 ||
        fail "release archive validation failed"
    for required in \
        VERSION TARGET LICENSE bin/hydra-codex \
        marketplace/.agents/plugins/marketplace.json \
        marketplace/plugins/hydra-codex/.codex-plugin/plugin.json
    do
        LC_ALL=C grep -Fx "$expected_top/$required" "$normalized" >/dev/null ||
            fail "release archive inventory is invalid"
    done
}

parse_arguments()
{
    MODE=install
    VERSION=
    case $# in
        0) ;;
        1)
            case "$1" in
                --check) MODE=check ;;
                --acquire)
                    capability=${HYDRA_INTERNAL_RELEASE_ACQUISITION-}
                    printf '%s\n' "$capability" |
                        LC_ALL=C grep -Eq '^[0-9a-f]{64}$' ||
                        fail "unsupported arguments"
                    ACQUISITION_CAPABILITY=$capability
                    MODE=acquire
                    ;;
                --resolve)
                    capability=${HYDRA_INTERNAL_RELEASE_RESOLUTION-}
                    printf '%s\n' "$capability" |
                        LC_ALL=C grep -Eq '^[0-9a-f]{64}$' ||
                        fail "unsupported arguments"
                    MODE=resolve
                    ;;
                --uninstall) MODE=uninstall ;;
                *) fail "unsupported arguments" ;;
            esac
            ;;
        2)
            [ "$1" = --version ] || fail "unsupported arguments"
            valid_version "$2" || fail "release version is invalid"
            VERSION=$2
            ;;
        *) fail "unsupported arguments" ;;
    esac
}

parse_arguments "$@"
validate_home

if [ "$MODE" = uninstall ]; then
    inspect_local_state
    [ -n "$INSTALLED_VERSION" ] || fail "Hydra is not installed"
    exec "$HOME/.hydra/current/bin/hydra-codex" uninstall
fi

detect_target
validate_release_base
inspect_local_state
if [ -n "$INSTALLED_VERSION" ] && [ "$BUNDLE_TARGET" != "$TARGET" ]; then
    fail "active release target does not match this platform"
fi

if [ "$MODE" = check ]; then
    resolve_latest
    if [ -z "$INSTALLED_VERSION" ]; then
        printf 'Hydra is not installed; latest is %s\n' "$VERSION"
    elif [ "$INSTALLED_VERSION" = "$VERSION" ]; then
        printf 'Hydra is up to date (%s)\n' "$VERSION"
    else
        printf 'Hydra update available: %s -> %s\n' \
            "$INSTALLED_VERSION" "$VERSION"
    fi
    exit 0
fi

if [ "$MODE" = resolve ]; then
    [ -n "$INSTALLED_VERSION" ] || fail "Hydra is not installed"
    resolve_latest
    printf '{"current_version":"%s","latest_version":"%s"}\n' \
        "$INSTALLED_VERSION" "$VERSION"
    exit 0
fi

if [ "$MODE" = acquire ]; then
    validate_acquisition_capability "$ACQUISITION_CAPABILITY"
fi

if [ -z "$VERSION" ]; then
    resolve_latest
fi

DOWNLOAD_DIR=$(mktemp -d "$HOME/.hydra-download.XXXXXXXX" 2>/dev/null) ||
    fail "private download staging failed"
archive_name=hydra-codex-$VERSION-$TARGET.tar.gz
archive=$DOWNLOAD_DIR/$archive_name
manifest=$DOWNLOAD_DIR/SHA256SUMS
download_prefix=$RELEASE_BASE/download/v$VERSION
curl_to_file "$download_prefix/$archive_name" "$archive" \
    "$download_prefix/$archive_name" 536870912
curl_to_file "$download_prefix/SHA256SUMS" "$manifest" \
    "$download_prefix/SHA256SUMS" 4096
validate_manifest "$manifest"
verify_checksum "$archive" "$manifest"
top=hydra-codex-$VERSION
preflight_archive "$archive" "$top"

inspect_local_state
if [ "$MODE" = acquire ]; then
    safe_private_directory "$HOME/.hydra"
    STAGE_DIR=$(mktemp -d "$HOME/.hydra/.acquire.XXXXXXXX" 2>/dev/null) ||
        fail "private release staging failed"
else
    STAGE_DIR=$DOWNLOAD_DIR/stage
    mkdir -m 700 "$STAGE_DIR" 2>/dev/null ||
        fail "private release staging failed"
fi
tar -xzf "$archive" -C "$STAGE_DIR" 2>/dev/null ||
    fail "release extraction failed"
staged=$STAGE_DIR/$top
validate_bundle_directory "$staged" "$VERSION" "$TARGET"
reported=$("$staged/bin/hydra-codex" --version 2>/dev/null) ||
    fail "staged executable validation failed"
[ "$reported" = "hydra-codex $VERSION" ] ||
    fail "staged executable version does not match"

if [ "$MODE" = acquire ]; then
    STAGE_DIR=
    printf '%s\n' "$staged"
    exit 0
fi

HYDRA_INTERNAL_INSTALLER_ACTIVATION=1 \
    "$staged/bin/hydra-codex" __installer-activate "$staged" \
    >/dev/null 2>/dev/null ||
    fail "release activation failed"
inspect_local_state
[ "$INSTALLED_VERSION" = "$VERSION" ] && [ "$BUNDLE_TARGET" = "$TARGET" ] ||
    fail "release activation failed"

printf 'Hydra %s installed successfully.\n' "$VERSION"
case ":${PATH-}:" in
    *':$HOME/.local/bin:'*) ;;
    *":$HOME/.local/bin:"*) ;;
    *)
        printf '%s\n' \
            'Add Hydra to PATH: export PATH="$HOME/.local/bin:$PATH"'
        ;;
esac
