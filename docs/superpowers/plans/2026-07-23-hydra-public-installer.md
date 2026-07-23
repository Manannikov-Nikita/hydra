# Hydra Public Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Hydra installable by any supported Codex user through one verified shell command, with safe project initialization, Codex integration, upgrades, uninstall, and public GitHub releases.

**Architecture:** A native PyInstaller one-folder runtime is wrapped by one stable `hydra-codex` launcher and installed into versioned user-owned directories. Project configuration, platform data paths, Codex marketplace reconciliation, release verification, and bundle construction remain separate dependency-free modules with injected adapters for deterministic tests. GitHub Actions builds each supported target natively, accepts the installed artifact outside the checkout, attests checksums, and only then publishes a release.

**Tech Stack:** Python 3.12, standard library, SQLite, PyInstaller 6.21, POSIX `sh`, Codex plugin marketplace CLI, GitHub Actions, `unittest`.

## Global Constraints

- Public repository: `https://github.com/Manannikov-Nikita/hydra`.
- Supported targets in the first release: `darwin-arm64`, `darwin-x86_64`, and `linux-x86_64`.
- Users must not need Python, pip, uv, or a virtual environment.
- Runtime layout: `~/.hydra/versions/<version>` with `~/.hydra/current` and `~/.local/bin/hydra-codex` as atomic symlinks.
- The only project-local artifact is `.hydra/project.toml`; installation must not create project-local Codex hooks.
- The bundled local Codex marketplace is the source of the plugin, hooks, MCP server, and `$hydra-report`.
- `init`, installation, refresh, upgrade, and uninstall are idempotent.
- `init` refuses the filesystem root, the user home, malformed existing configuration, and a symlinked `.hydra` path.
- macOS data stays under `~/Library/Application Support/Hydra`; Linux uses `<absolute XDG_DATA_HOME>/hydra` or `~/.local/share/hydra`.
- Full uninstall removes only proven-owned executables and integration state; it preserves the telemetry database, installation key, spool, receipts, and project files.
- All downloaded archives are verified against an exact SHA-256 entry before extraction.
- Release assets are built only from a clean exact tag and never from the ignored local `dist/` directory.
- Dynamic prompts, raw assistant messages, raw tool output, and private absolute paths must not be added to installer diagnostics or receipts.
- Existing wheel console scripts remain available for developer compatibility, but all bundled runtime configuration uses the single `hydra-codex` executable.
- Implementation uses RED → GREEN TDD for every behavior change and an independent review gate after each task.

---

### Task 1: Canonical Version, Public Metadata, and Platform Data Paths

**Files:**
- Create: `src/hydra_codex/platform_paths.py`
- Create: `tests/test_platform_paths.py`
- Create: `tests/test_release_metadata.py`
- Create: `LICENSE`
- Modify: `src/hydra_codex/__init__.py`
- Modify: `src/hydra_codex/cli.py`
- Modify: `src/hydra_codex/mcp_server.py`
- Modify: `src/hydra_codex/storage.py`
- Modify: `src/hydra_codex/services.py`
- Modify: `src/hydra_codex/hook_runtime.py`
- Modify: `src/hydra_codex/dashboard_launch.py`
- Modify: `pyproject.toml`
- Test: `tests/test_storage.py`
- Test: `tests/test_local_services.py`
- Test: `tests/test_codex_hooks.py`

**Interfaces:**
- Consumes: existing environment override names `HYDRA_DATABASE_PATH` and `HYDRA_INSTALLATION_KEY_PATH`.
- Produces:

```python
from collections.abc import Mapping
from pathlib import Path

def default_data_directory(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path: ...

def default_database_path(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path: ...

def default_installation_key_path(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path: ...
```

- `hydra_codex.__version__` is the sole version source.
- `hydra-codex --version` writes exactly `hydra-codex <version>\n`.
- MCP `serverInfo.version`, wheel metadata, bundle `VERSION`, marketplace metadata, and Git tag all consume the same version.

- [ ] **Step 1: Write the failing path and metadata tests**

```python
class PlatformPathTests(unittest.TestCase):
    def test_macos_preserves_application_support_path(self) -> None:
        self.assertEqual(
            default_data_directory(Path("/Users/test"), platform="darwin"),
            Path("/Users/test/Library/Application Support/Hydra"),
        )

    def test_linux_uses_absolute_xdg_data_home(self) -> None:
        self.assertEqual(
            default_data_directory(
                Path("/home/test"),
                platform="linux",
                environ={"XDG_DATA_HOME": "/data/test"},
            ),
            Path("/data/test/hydra"),
        )

    def test_linux_ignores_relative_xdg_data_home(self) -> None:
        self.assertEqual(
            default_data_directory(
                Path("/home/test"),
                platform="linux",
                environ={"XDG_DATA_HOME": "relative"},
            ),
            Path("/home/test/.local/share/hydra"),
        )

class ReleaseMetadataTests(unittest.TestCase):
    def test_cli_and_mcp_use_package_version(self) -> None:
        self.assertEqual(run_cli("--version"), f"hydra-codex {__version__}\n")
        self.assertEqual(server_initialize()["serverInfo"]["version"], __version__)

    def test_public_metadata_and_license_are_shipped(self) -> None:
        metadata = load_pyproject()
        self.assertEqual(metadata["license"]["text"], "MIT")
        self.assertEqual(
            metadata["urls"]["Repository"],
            "https://github.com/Manannikov-Nikita/hydra",
        )
        self.assertIn("LICENSE", wheel_members())
        self.assertIn("LICENSE", sdist_members())
```

- [ ] **Step 2: Run the focused tests and capture RED**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_platform_paths \
  tests.test_release_metadata \
  tests.test_storage \
  tests.test_local_services \
  tests.test_codex_hooks -v
```

Expected: FAIL because `platform_paths`, public metadata, `LICENSE`, and global `--version` do not exist.

- [ ] **Step 3: Implement the platform path and version boundaries**

```python
def default_data_directory(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    values = dict(os.environ if environ is None else environ)
    resolved_home = Path.home() if home is None else home
    current_platform = sys.platform if platform is None else platform
    if current_platform == "darwin":
        return resolved_home / "Library/Application Support/Hydra"
    if current_platform.startswith("linux"):
        candidate = values.get("XDG_DATA_HOME")
        base = Path(candidate) if candidate and Path(candidate).is_absolute() else resolved_home / ".local/share"
        return base / "hydra"
    raise RuntimeError(f"unsupported platform: {current_platform}")

def default_database_path(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    return default_data_directory(home, environ=environ, platform=platform) / "hydra.sqlite3"

def default_installation_key_path(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    return default_data_directory(home, environ=environ, platform=platform) / "rollout-hmac.key"
```

Use `dynamic = ["version"]` and `[tool.setuptools.dynamic] version = {attr = "hydra_codex.__version__"}` in `pyproject.toml`. Add the standard MIT text with `Copyright (c) 2026 Nikita Manannikov`, SPDX metadata, repository/issues URLs, OS classifiers, and the public description `Local, privacy-preserving telemetry and evidence for Codex.`

- [ ] **Step 4: Run focused and full regression tests**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_platform_paths \
  tests.test_release_metadata \
  tests.test_storage \
  tests.test_local_services \
  tests.test_codex_hooks -v
env PYTHONPATH="$PWD/src" python3.12 -m unittest discover -s tests -t .
```

Expected: all tests PASS; existing macOS storage assertions remain byte-for-byte unchanged.

- [ ] **Step 5: Commit**

```bash
git add LICENSE pyproject.toml src/hydra_codex tests
git commit -m "feat(storage): add portable public runtime metadata"
```

---

### Task 2: Safe Project Identity, Init, Status, and Uninit

**Files:**
- Create: `src/hydra_codex/project_config.py`
- Create: `src/hydra_codex/project_lifecycle.py`
- Create: `src/hydra_codex/status.py`
- Create: `src/hydra_codex/installation_cli.py`
- Create: `tests/test_project_config.py`
- Create: `tests/test_project_lifecycle.py`
- Create: `tests/test_status.py`
- Modify: `src/hydra_codex/project.py`
- Modify: `src/hydra_codex/cli.py`
- Test: `tests/test_project.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `default_database_path()` and `default_installation_key_path()` from Task 1.
- Produces:

```python
PROJECT_CONFIG_SCHEMA_VERSION = 1
PROJECT_ID_PATTERN = re.compile(r"\Ahprj_[0-9a-f]{16}\Z")

@dataclass(frozen=True)
class ProjectConfig:
    schema_version: int | None
    project_id: str
    display_name: str | None
    telemetry: str | None

@dataclass(frozen=True)
class ProjectMutationResult:
    project_root: Path
    project_id: str
    changed: bool

@dataclass(frozen=True)
class ProjectStatus:
    initialized: bool
    identity_valid: bool | None
    config_schema_version: int | None
    project_root: Path | None = field(repr=False)

def generate_project_id(
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> str: ...

def parse_project_config(raw: bytes, *, source: Path) -> ProjectConfig: ...
def render_project_config(config: ProjectConfig) -> bytes: ...
def initialize_project(path: Path | str = ".", *, name: str | None = None) -> ProjectMutationResult: ...
def uninitialize_project(path: Path | str = ".", *, confirmation: str) -> ProjectMutationResult: ...
def collect_status(path: Path | str = ".", *, environ: Mapping[str, str]) -> dict[str, object]: ...
```

- New IDs are `hprj_` plus 16 lowercase hexadecimal characters.
- Legacy files without `schema_version` remain readable if their ID is canonical.
- Target resolution is: existing nearest Hydra root, otherwise nearest Git root, otherwise the exact passed directory.
- Status is read-only and returns exit 0 for an uninitialized project.

- [ ] **Step 1: Write failing identity and lifecycle tests**

```python
class ProjectConfigTests(unittest.TestCase):
    def test_generated_identity_is_canonical(self) -> None:
        self.assertRegex(generate_project_id(lambda size: b"\xab" * size), r"^hprj_[0-9a-f]{16}$")

    def test_unknown_fields_fail_closed(self) -> None:
        with self.assertRaises(ProjectConfigError):
            parse_project_config(
                b'project_id = "hprj_0123456789abcdef"\nunknown = true\n',
                source=Path("project.toml"),
            )

class ProjectLifecycleTests(unittest.TestCase):
    def test_concurrent_init_converges_on_one_identity(self) -> None:
        results = run_two_initializers(self.project)
        self.assertEqual({result.project_id for result in results}, {read_id(self.project)})
        self.assertEqual(count_temp_files(self.project), 0)

    def test_repeated_init_preserves_bytes(self) -> None:
        first = initialize_project(self.project)
        original = config_bytes(self.project)
        second = initialize_project(self.project)
        self.assertFalse(second.changed)
        self.assertEqual(config_bytes(self.project), original)
        self.assertEqual(second.project_id, first.project_id)

    def test_protected_targets_are_rejected(self) -> None:
        for target in (Path("/"), self.home):
            with self.subTest(target=target), self.assertRaises(UnsafeProjectTarget):
                initialize_project(target, home=self.home)

class StatusTests(unittest.TestCase):
    def test_uninitialized_status_is_successful_and_read_only(self) -> None:
        before = inventory(self.root)
        result = collect_status(self.root, environ=self.environ)
        self.assertFalse(result["project"]["initialized"])
        self.assertEqual(inventory(self.root), before)
```

- [ ] **Step 2: Run focused tests and capture RED**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_project_config \
  tests.test_project_lifecycle \
  tests.test_status \
  tests.test_project \
  tests.test_cli -v
```

Expected: FAIL because the project lifecycle modules and CLI commands do not exist.

- [ ] **Step 3: Implement strict parsing and exclusive atomic creation**

```python
def generate_project_id(
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> str:
    return f"hprj_{random_bytes(8).hex()}"

def initialize_project(
    path: Path | str = ".",
    *,
    name: str | None = None,
    project_id_factory: Callable[[], str] = generate_project_id,
    home: Path | None = None,
) -> ProjectMutationResult:
    root = canonical_project_target(path, home=home)
    config_path = root / ".hydra/project.toml"
    if config_path.exists():
        current = read_project_config(config_path)
        return ProjectMutationResult(root, current.project_id, False)
    config = ProjectConfig(1, project_id_factory(), normalize_name(name, root), "hybrid")
    publish_exclusively(config_path, render_project_config(config))
    current = read_project_config(config_path)
    return ProjectMutationResult(root, current.project_id, current.project_id == config.project_id)
```

`publish_exclusively()` must write a mode-600 temporary file in `.hydra`, `flush()`, `os.fsync()`, then use an exclusive hard-link/create-if-absent operation so parallel writers cannot replace each other. It must fsync the directory and remove its own temporary file on every path.

`uninitialize_project()` requires exact text `remove hydra project`, removes only a valid `.hydra/project.toml`, and removes `.hydra` only when empty. `collect_status()` must inspect SQLite through `mode=ro` URI only when the database exists; it must never construct `HydraStore`.

- [ ] **Step 4: Wire and verify the CLI contract**

Add:

```text
hydra-codex init [PATH] [--name NAME]
hydra-codex status [PATH] [--json]
hydra-codex uninit [PATH] --confirmation "remove hydra project"
```

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_project_config \
  tests.test_project_lifecycle \
  tests.test_status \
  tests.test_project \
  tests.test_cli -v
env PYTHONPATH="$PWD/src" python3.12 -m unittest discover -s tests -t .
```

Expected: all tests PASS, uninitialized `status --json` exits 0, malformed state exits 2 without leaking absolute private paths.

- [ ] **Step 5: Commit**

```bash
git add src/hydra_codex/project.py src/hydra_codex/project_config.py \
  src/hydra_codex/project_lifecycle.py src/hydra_codex/status.py \
  src/hydra_codex/installation_cli.py src/hydra_codex/cli.py tests
git commit -m "feat(project): add safe initialization lifecycle"
```

---

### Task 3: One Frozen Runtime Entrypoint and Bundled Marketplace

**Files:**
- Create: `src/hydra_codex/runtime_entrypoint.py`
- Create: `src/hydra_codex/install_layout.py`
- Create: `.agents/plugins/marketplace.json`
- Create: `tests/test_runtime_entrypoint.py`
- Create: `tests/test_install_layout.py`
- Modify: `src/hydra_codex/cli.py`
- Modify: `src/hydra_codex/plugin_bundle.py`
- Modify: `src/hydra_codex/hook_runtime.py`
- Modify: `src/hydra_codex/mcp_server.py`
- Modify: `plugins/hydra-codex/hooks/hooks.json`
- Modify: `plugins/hydra-codex/.mcp.json`
- Modify: `pyproject.toml`
- Test: `tests/test_codex_hooks.py`
- Test: `tests/test_mcp_server.py`
- Test: `tests/test_plugin_distribution.py`
- Test: `tests/test_plugin_hook_packaging.py`

**Interfaces:**
- Consumes: `hydra_codex.__version__`.
- Produces:

```python
SUPPORTED_TARGETS = ("darwin-arm64", "darwin-x86_64", "linux-x86_64")

@dataclass(frozen=True)
class BundleLayout:
    root: Path
    version: str
    target: str
    executable: Path
    marketplace: Path

def runtime_command_prefix(*, executable: Path | None = None, frozen: bool | None = None) -> tuple[str, ...]: ...
def platform_target(system: str, machine: str) -> str: ...
def frozen_bundle_root(executable: Path | None = None) -> Path | None: ...
def validate_bundle(root: Path, *, expected_version: str | None = None, expected_target: str | None = None) -> BundleLayout: ...
def marketplace_root_path() -> Path: ...
```

- `hydra-codex hook` replaces checkout-specific hook commands.
- `hydra-codex mcp` replaces bare legacy MCP scripts in plugin configuration.
- Wheel-only console scripts `hydra-codex-hook`, `hydra-codex-mcp`, and `hydra-codex-plugin` remain.

- [ ] **Step 1: Write failing launcher, layout, and manifest tests**

```python
class RuntimeEntrypointTests(unittest.TestCase):
    def test_source_runtime_uses_current_interpreter_module(self) -> None:
        self.assertEqual(
            runtime_command_prefix(executable=Path("/venv/bin/python"), frozen=False),
            ("/venv/bin/python", "-m", "hydra_codex"),
        )

    def test_frozen_runtime_uses_single_executable(self) -> None:
        self.assertEqual(
            runtime_command_prefix(executable=Path("/bundle/bin/hydra-codex"), frozen=True),
            ("/bundle/bin/hydra-codex",),
        )

class InstallLayoutTests(unittest.TestCase):
    def test_validate_bundle_requires_matching_version_target_and_marketplace(self) -> None:
        layout = validate_bundle(self.bundle, expected_version=__version__, expected_target="darwin-arm64")
        self.assertEqual(layout.marketplace, self.bundle / "marketplace")

    def test_platform_allowlist_rejects_unknown_architecture(self) -> None:
        with self.assertRaises(UnsupportedTarget):
            platform_target("linux", "aarch64")

class PluginManifestTests(unittest.TestCase):
    def test_installed_manifests_use_one_public_executable(self) -> None:
        self.assertEqual(hook_command(), "env HYDRA_CODEX_HOOK_SOURCE=plugin hydra-codex hook")
        self.assertEqual(mcp_command(), ("hydra-codex", "mcp"))
```

- [ ] **Step 2: Run tests and capture RED**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_runtime_entrypoint \
  tests.test_install_layout \
  tests.test_codex_hooks \
  tests.test_mcp_server \
  tests.test_plugin_distribution \
  tests.test_plugin_hook_packaging -v
```

Expected: FAIL because layout/runtime modules and `hook`/`mcp` CLI routes do not exist and manifests still use legacy scripts.

- [ ] **Step 3: Implement runtime routing and frozen bundle discovery**

```python
def runtime_command_prefix(
    *,
    executable: Path | None = None,
    frozen: bool | None = None,
) -> tuple[str, ...]:
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    selected = Path(sys.executable) if executable is None else executable
    return (str(selected),) if is_frozen else (str(selected), "-m", "hydra_codex")

def platform_target(system: str, machine: str) -> str:
    key = (system.lower(), machine.lower())
    targets = {
        ("darwin", "arm64"): "darwin-arm64",
        ("darwin", "x86_64"): "darwin-x86_64",
        ("linux", "x86_64"): "linux-x86_64",
        ("linux", "amd64"): "linux-x86_64",
    }
    try:
        return targets[key]
    except KeyError as error:
        raise UnsupportedTarget(f"unsupported target: {system}/{machine}") from error
```

`plugin_bundle_path()` and `marketplace_root_path()` must first validate `<frozen-release>/marketplace`, then use checkout/wheel candidates. Hook instructions use a command prefix injected by the caller. MCP subprocesses use an injected tuple prefix, `shell=False`, a bounded timeout, and bounded captured output.

- [ ] **Step 4: Verify source, frozen, and compatibility paths**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_runtime_entrypoint \
  tests.test_install_layout \
  tests.test_codex_hooks \
  tests.test_mcp_server \
  tests.test_plugin_distribution \
  tests.test_plugin_hook_packaging -v
env PYTHONPATH="$PWD/src" python3.12 -m unittest discover -s tests -t .
```

Expected: all tests PASS; adversarial strings such as `echo hydra-codex-hook` do not suppress the trusted hook.

- [ ] **Step 5: Commit**

```bash
git add .agents/plugins/marketplace.json pyproject.toml \
  plugins/hydra-codex src/hydra_codex tests
git commit -m "refactor(runtime): unify installed Hydra entrypoints"
```

---

### Task 4: Idempotent Codex Integration Reconciliation

**Files:**
- Create: `src/hydra_codex/codex_integration.py`
- Create: `tests/test_codex_integration.py`
- Modify: `src/hydra_codex/installation_cli.py`
- Modify: `src/hydra_codex/cli.py`
- Modify: `src/hydra_codex/plugin_bundle.py`
- Modify: `src/hydra_codex/status.py`
- Modify: `plugins/hydra-codex/README.md`
- Test: `tests/test_cli.py`
- Test: `tests/test_plugin_distribution.py`
- Test: `tests/test_status.py`

**Interfaces:**
- Consumes: `marketplace_root_path()`, `runtime_command_prefix()`, `default_data_directory()`, `hydra_codex.__version__`.
- Produces:

```python
@dataclass(frozen=True)
class MarketplaceRecord:
    name: str
    source: Path

@dataclass(frozen=True)
class PluginRecord:
    name: str
    marketplace: str
    installed: bool
    version: str | None

@dataclass(frozen=True)
class IntegrationReport:
    changed: bool
    marketplace: str
    selector: str
    runtime_version: str

class CodexClient(Protocol):
    def version(self) -> str: ...
    def list_marketplaces(self) -> tuple[MarketplaceRecord, ...]: ...
    def add_marketplace(self, root: Path) -> None: ...
    def remove_marketplace(self, name: str) -> None: ...
    def list_plugins(self, marketplace: str, *, include_available: bool) -> tuple[PluginRecord, ...]: ...
    def add_plugin(self, selector: str) -> None: ...
    def remove_plugin(self, selector: str) -> None: ...

def configure_codex(
    *,
    client: CodexClient,
    marketplace_root: Path,
    runtime_version: str,
    receipt_path: Path,
    refresh: bool,
) -> IntegrationReport: ...

def remove_codex_integration(*, client: CodexClient, receipt_path: Path) -> IntegrationReport: ...
def render_codex_config(*, marketplace_root: Path, runtime_version: str) -> str: ...
```

- Supported Codex commands are:

```text
codex --version
codex plugin marketplace list --json
codex plugin marketplace add <marketplace-root> --json
codex plugin marketplace remove hydra --json
codex plugin list --marketplace hydra --available --json
codex plugin add hydra-codex@hydra --json
codex plugin remove hydra-codex@hydra --json
```

- No code directly edits `~/.codex/config.toml`.
- Codex compatibility is capability-based: `codex --version`, marketplace listing, and plugin listing must execute successfully and return the documented JSON shapes; Hydra does not invent an unverified numeric version floor.

- [ ] **Step 1: Write failing integration state-machine tests**

```python
class CodexIntegrationTests(unittest.TestCase):
    def test_fresh_install_adds_marketplace_plugin_and_private_receipt(self) -> None:
        report = configure_codex(
            client=self.client,
            marketplace_root=self.marketplace,
            runtime_version="0.1.0",
            receipt_path=self.receipt,
            refresh=False,
        )
        self.assertTrue(report.changed)
        self.assertEqual(
            self.client.calls,
            [("add_marketplace", self.marketplace), ("add_plugin", "hydra-codex@hydra")],
        )
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)

    def test_exact_repeat_is_a_noop(self) -> None:
        self.install_once()
        self.client.calls.clear()
        report = self.install_once()
        self.assertFalse(report.changed)
        self.assertEqual(self.client.calls, [])

    def test_refresh_failure_restores_previous_integration_and_receipt(self) -> None:
        original = self.install_once(version="0.1.0")
        original_receipt = self.receipt.read_bytes()
        self.client.fail_on = ("add_plugin", "hydra-codex@hydra")
        with self.assertRaises(IntegrationError):
            self.install_once(version="0.2.0", refresh=True)
        self.assertEqual(self.receipt.read_bytes(), original_receipt)
        self.assertInstalled(version=original.runtime_version)

    def test_uninstall_never_touches_unowned_marketplace(self) -> None:
        self.client.marketplaces["hydra"] = Path("/foreign")
        with self.assertRaises(IntegrationOwnershipError):
            remove_codex_integration(client=self.client, receipt_path=self.receipt)
        self.assertEqual(self.client.calls, [])

    def test_incompatible_codex_fails_before_mutation(self) -> None:
        self.client.plugin_listing_supported = False
        with self.assertRaises(IncompatibleCodexError):
            self.install_once()
        self.assertEqual(self.client.mutation_calls, [])
```

- [ ] **Step 2: Run tests and capture RED**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_codex_integration \
  tests.test_cli \
  tests.test_plugin_distribution \
  tests.test_status -v
```

Expected: FAIL because Codex reconciliation and installation commands do not exist.

- [ ] **Step 3: Implement exact-state reconciliation and rollback**

```python
def configure_codex(
    *,
    client: CodexClient,
    marketplace_root: Path,
    runtime_version: str,
    receipt_path: Path,
    refresh: bool,
) -> IntegrationReport:
    desired = desired_state(marketplace_root, runtime_version)
    current = inspect_state(client, desired)
    owned = read_receipt_if_present(receipt_path)
    if current == desired and owned == desired.receipt:
        return IntegrationReport(False, "hydra", "hydra-codex@hydra", runtime_version)
    ensure_reconcilable(current, owned, refresh=refresh)
    previous = snapshot_owned_state(current, owned)
    try:
        reconcile_to_desired(client, current, desired)
        verify_exact_state(client, desired)
        write_private_receipt_atomically(receipt_path, desired.receipt)
    except Exception:
        restore_owned_state(client, previous)
        raise
    return IntegrationReport(True, "hydra", "hydra-codex@hydra", runtime_version)
```

The subprocess client must parse JSON strictly, bound timeout/output, preserve a minimal safe `PATH`, and redact absolute paths from user-facing failures. The receipt records marketplace name, canonical source, selector, runtime version, and schema version. After reconciliation, `collect_status()` reports Codex availability, marketplace/plugin version parity, whether a new Codex task is required, and privacy-safe next actions without mutating Codex.

- [ ] **Step 4: Wire installation commands and verify**

Add:

```text
hydra-codex install [-y] [--refresh] [--print-config codex]
hydra-codex uninstall [--keep-cli]
```

`--print-config codex` is read-only and needs neither Codex nor confirmation. Mutation requires `-y` or an interactive confirmation on a TTY.

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_codex_integration \
  tests.test_cli \
  tests.test_plugin_distribution \
  tests.test_status -v
env PYTHONPATH="$PWD/src" python3.12 -m unittest discover -s tests -t .
```

Expected: all tests PASS; fake client confirms exact call order and rollback.

- [ ] **Step 5: Commit**

```bash
git add src/hydra_codex/codex_integration.py \
  src/hydra_codex/installation_cli.py src/hydra_codex/cli.py \
  src/hydra_codex/plugin_bundle.py src/hydra_codex/status.py \
  plugins/hydra-codex/README.md tests
git commit -m "feat(integration): install Hydra into Codex safely"
```

---

### Task 5: Verified Version Layout, Upgrade, Rollback, and Uninstall

**Files:**
- Create: `src/hydra_codex/release_management.py`
- Create: `tests/test_release_management.py`
- Modify: `src/hydra_codex/installation_cli.py`
- Modify: `src/hydra_codex/cli.py`
- Modify: `src/hydra_codex/status.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_status.py`

**Interfaces:**
- Consumes: `BundleLayout`, `validate_bundle()`, `configure_codex()`, `remove_codex_integration()`.
- Produces:

```python
@dataclass(frozen=True)
class InstallRoots:
    home: Path
    versions: Path
    current: Path
    launcher: Path

@dataclass(frozen=True)
class UpgradeStatus:
    current_version: str
    latest_version: str
    update_available: bool

def default_install_roots(home: Path | None = None) -> InstallRoots: ...
def activate_version(layout: BundleLayout, *, roots: InstallRoots) -> Path: ...
def upgrade(*, check: bool, environ: Mapping[str, str], stdout: TextIO) -> UpgradeStatus: ...
def uninstall(*, keep_cli: bool, environ: Mapping[str, str], detach_integration: Callable[[], None]) -> None: ...
```

- `activate_version()` only replaces symlinks already owned by Hydra.
- Upgrade preserves the previous release and rolls `current` back if Codex refresh fails.
- Uninstall never deletes the data directory or project `.hydra` directories.

CLI surface:

```text
hydra-codex upgrade [--check]
hydra-codex uninstall [--keep-cli]
```

- [ ] **Step 1: Write failing lifecycle tests**

```python
class ReleaseManagementTests(unittest.TestCase):
    def test_activation_refuses_unrelated_launcher(self) -> None:
        self.roots.launcher.write_text("# foreign\n", encoding="utf-8")
        with self.assertRaises(InstallOwnershipError):
            activate_version(self.layout, roots=self.roots)
        self.assertEqual(self.roots.launcher.read_text(), "# foreign\n")

    def test_refresh_failure_rolls_current_back(self) -> None:
        self.activate("0.1.0")
        previous = self.roots.current.resolve()
        with self.assertRaises(IntegrationError):
            self.upgrade_to("0.2.0", fail_refresh=True)
        self.assertEqual(self.roots.current.resolve(), previous)
        self.assertTrue((self.roots.versions / "0.2.0").exists())

    def test_uninstall_preserves_telemetry_and_project_state(self) -> None:
        before = private_data_inventory(self.data_root, self.project)
        uninstall(keep_cli=False, environ=self.environ, detach_integration=self.detach)
        self.assertEqual(private_data_inventory(self.data_root, self.project), before)
        self.assertFalse(self.roots.current.exists())
```

- [ ] **Step 2: Run tests and capture RED**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_release_management \
  tests.test_cli \
  tests.test_status -v
```

Expected: FAIL because release lifecycle functions and `upgrade` are absent.

- [ ] **Step 3: Implement owned atomic activation and rollback**

```python
def activate_version(layout: BundleLayout, *, roots: InstallRoots) -> Path:
    roots.versions.mkdir(parents=True, mode=0o700, exist_ok=True)
    version_root = roots.versions / layout.version
    if version_root.exists():
        validate_bundle(version_root, expected_version=layout.version, expected_target=layout.target)
    else:
        os.replace(layout.root, version_root)
    replace_owned_symlink_atomically(roots.current, version_root, owner_root=roots.versions)
    replace_owned_symlink_atomically(
        roots.launcher,
        roots.current / "bin/hydra-codex",
        owner_root=roots.home,
    )
    return version_root
```

`upgrade()` invokes the verified bundled installer, records the previous `current` target, calls `configure_codex(refresh=True)`, and restores `current` on failure. `uninstall()` first detaches Codex, then removes only matching Hydra links and version roots. `--keep-cli` stops after integration detachment. `collect_status()` adds active installation version and target after validating `current`; missing or malformed installation state remains a read-only diagnostic.

- [ ] **Step 4: Verify repeated and failure paths**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_release_management \
  tests.test_cli \
  tests.test_status -v
env PYTHONPATH="$PWD/src" python3.12 -m unittest discover -s tests -t .
```

Expected: all tests PASS; install v1, failed v2, successful v2, repeat v2, rollback, `--keep-cli`, and full uninstall are covered.

- [ ] **Step 5: Commit**

```bash
git add src/hydra_codex/release_management.py \
  src/hydra_codex/installation_cli.py src/hydra_codex/cli.py \
  src/hydra_codex/status.py tests/test_release_management.py \
  tests/test_cli.py tests/test_status.py
git commit -m "feat(updater): add owned release lifecycle"
```

---

### Task 6: One-Line POSIX Installer with Safe Archive Extraction

**Files:**
- Create: `install.sh`
- Create: `src/hydra_codex/archive_validation.py`
- Create: `tests/test_installer.py`
- Create: `tests/test_archive_validation.py`

**Interfaces:**
- Consumes: archive naming and layout from Tasks 3 and 5.
- Produces:

```python
@dataclass(frozen=True)
class ValidatedArchive:
    archive: Path
    top_level: str
    version: str
    target: str

def validate_tar_members(
    archive: Path,
    *,
    expected_top_level: str,
) -> ValidatedArchive: ...
```

Shell interface:

```text
sh install.sh
sh install.sh --version 0.1.0
sh install.sh --check
sh install.sh --uninstall
```

`HYDRA_INSTALLER_RELEASE_BASE_URL` is accepted only as a test seam for a local fake release server.

- [ ] **Step 1: Write failing installer security and lifecycle tests**

```python
class ArchiveValidationTests(unittest.TestCase):
    def test_rejects_escaping_and_duplicate_critical_members(self) -> None:
        for archive in (
            make_tar("../escape"),
            make_tar("/absolute"),
            make_symlink_tar("runtime/link", "../../escape"),
            make_duplicate_tar("VERSION"),
        ):
            with self.subTest(archive=archive), self.assertRaises(UnsafeArchive):
                validate_tar_members(archive, expected_top_level="hydra-codex-0.1.0")

class InstallerTests(unittest.TestCase):
    def test_checksum_mismatch_never_extracts(self) -> None:
        result = self.run_installer(checksum="0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.install_root.exists())

    def test_installs_exact_asset_and_is_idempotent(self) -> None:
        first = self.run_installer(version="0.1.0")
        second = self.run_installer(version="0.1.0")
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(self.launcher.resolve(), self.version_launcher("0.1.0"))

    def test_unrelated_launcher_is_preserved(self) -> None:
        self.launcher.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
        result = self.run_installer(version="0.1.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exit 42", self.launcher.read_text())
```

- [ ] **Step 2: Run tests and capture RED**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_archive_validation \
  tests.test_installer -v
```

Expected: FAIL because `install.sh` and archive validation are absent.

- [ ] **Step 3: Implement the verified installer sequence**

The shell script must execute this exact order:

```text
1. Parse only --version, --check, and --uninstall.
2. Map uname to the three supported target strings.
3. Resolve the latest release or explicit version.
4. Download exactly one archive and SHA256SUMS into a mode-700 temporary directory.
5. Match exactly one lowercase 64-hex checksum line for the exact filename.
6. Verify with sha256sum or shasum -a 256.
7. Reject unsafe member names and every symlink/hard-link entry from `tar` listings before extraction; this POSIX preflight mirrors `validate_tar_members()` without requiring Python.
8. Extract into ~/.hydra/.staging-<random>.
9. Validate VERSION, TARGET, executable, marketplace, and LICENSE.
10. Run staged hydra-codex --version and require exact equality.
11. Move to versions/<version>, atomically update current, then create the owned launcher.
12. Preserve the previous version and print exact PATH guidance when ~/.local/bin is absent.
```

The shell implementation must use `set -eu`, quote every expansion, reject empty HOME, use `umask 077`, and install a trap that removes only its own resolved staging/download directories. `validate_tar_members()` is reused by Python-driven upgrades and builder tests; the initial shell installer enforces the same contract through `tar -tzf` and `tar -tvzf` before extraction because a first-time user has no Hydra Python runtime yet.

- [ ] **Step 4: Verify supported targets and hostile archives**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_archive_validation \
  tests.test_installer -v
shellcheck install.sh
```

Expected: all tests PASS and ShellCheck reports no errors. If ShellCheck is unavailable locally, the unit tests still run and the GitHub Quality workflow added in Task 8 must run ShellCheck.

- [ ] **Step 5: Commit**

```bash
git add install.sh src/hydra_codex/archive_validation.py \
  tests/test_archive_validation.py tests/test_installer.py
git commit -m "feat(installer): add verified standalone bootstrap"
```

---

### Task 7: Native Standalone Bundle and Clean-Machine Acceptance

**Files:**
- Create: `packaging/hydra-codex.spec`
- Create: `packaging/build_standalone.py`
- Create: `packaging/accept_standalone.sh`
- Create: `tests/test_standalone_build.py`
- Modify: `pyproject.toml`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: validated marketplace, version, target, public launcher, `install.sh`.
- Produces:

```python
def build_bundle(source_root: Path, output: Path, target: str) -> Path: ...
def create_archive(bundle_root: Path, output: Path) -> Path: ...
def sha256_file(path: Path) -> str: ...
```

Archive:

```text
hydra-codex-<version>-<target>.tar.gz
└── hydra-codex-<version>/
    ├── VERSION
    ├── TARGET
    ├── LICENSE
    ├── install.sh
    ├── bin/hydra-codex
    ├── runtime/hydra-codex/hydra-codex
    ├── runtime/hydra-codex/_internal/...
    └── marketplace/.agents/plugins/marketplace.json
```

- [ ] **Step 1: Write failing deterministic bundle tests**

```python
class StandaloneBuildTests(unittest.TestCase):
    def test_builder_refuses_nonempty_publication_directory(self) -> None:
        self.output.mkdir()
        (self.output / "stale").write_text("foreign", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            build_bundle(ROOT, self.output, "darwin-arm64")

    def test_archive_contains_one_valid_top_level_bundle(self) -> None:
        bundle = fixture_bundle(self.root, version="0.1.0", target="darwin-arm64")
        archive = create_archive(bundle, self.output)
        members = archive_members(archive)
        self.assertEqual({name.split("/", 1)[0] for name in members}, {"hydra-codex-0.1.0"})
        self.assertRequiredRuntimeAndMarketplaceMembers(members)

    def test_archive_digest_is_reproducible(self) -> None:
        first = create_archive(self.bundle, self.output / "one")
        second = create_archive(self.bundle, self.output / "two")
        self.assertEqual(sha256_file(first), sha256_file(second))
```

- [ ] **Step 2: Run tests and capture RED**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest tests.test_standalone_build -v
```

Expected: FAIL because packaging scripts and spec do not exist.

- [ ] **Step 3: Implement one-folder bundling**

Pin `pyinstaller==6.21.0` in a `release` optional dependency. The spec uses one-folder mode, explicitly collects dashboard assets and metadata, and does not place the marketplace inside `_internal`. `build_standalone.py` stages only from tracked inputs into a caller-selected fresh directory and normalizes tar ordering, timestamps, uid/gid, user/group names, and permissions before gzip compression.

Use PyInstaller's documented one-folder and frozen-runtime mechanisms:

- https://pyinstaller.org/en/stable/usage.html
- https://pyinstaller.org/en/latest/spec-files.html

- [ ] **Step 4: Build and accept the native local artifact**

Run:

```bash
python3.12 -m PyInstaller --noconfirm --clean packaging/hydra-codex.spec
python3.12 packaging/build_standalone.py --output "$PWD/release"
sh packaging/accept_standalone.sh "$PWD"/release/hydra-codex-*.tar.gz
```

The acceptance script must create a fresh HOME and foreign Git repository, unset `PYTHONPATH`/`PYTHONHOME`, prepend failing `python`, `python3`, `python3.12`, `pip`, and `uv` shims returning 97, install from a local fake release server, and verify:

```text
--version
install -y
init .
status . --json
hook
mcp
ingest
reconcile
dashboard health over loopback
upgrade to a second fixture release
uninstall while database remains
```

Expected: the installed Hydra subprocesses succeed only through `~/.local/bin/hydra-codex`; every Python shim remains unused.

- [ ] **Step 5: Run full regressions and commit**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest discover -s tests -t .
python3.12 -m build --no-isolation
```

Expected: source suite, wheel, sdist, native artifact, and clean install acceptance PASS.

```bash
git add packaging pyproject.toml .gitignore tests/test_standalone_build.py
git commit -m "build(distribution): package standalone Hydra"
```

---

### Task 8: Public Quality, Attested Release, and User Documentation

**Files:**
- Create: `.github/workflows/quality.yml`
- Create: `.github/workflows/release.yml`
- Create: `tests/test_workflow_contract.py`
- Create: `docs/installation.md`
- Create: `docs/upgrade-and-uninstall.md`
- Create: `docs/privacy.md`
- Create: `docs/troubleshooting.md`
- Create: `docs/release-process.md`
- Modify: `README.md`
- Modify: `plugins/hydra-codex/README.md`
- Modify: `tests/test_readme_contract.py`

**Interfaces:**
- Consumes: all commands and build/acceptance scripts from Tasks 1–7.
- Produces: exact tag-to-version release contract, three archives, `SHA256SUMS`, GitHub artifact attestations, and public installation/runbooks.

- [ ] **Step 1: Write failing workflow and documentation contracts**

```python
class WorkflowContractTests(unittest.TestCase):
    def test_release_is_tag_only_and_builds_exact_targets(self) -> None:
        workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn('tags: ["v*"]', workflow)
        for target in ("darwin-arm64", "darwin-x86_64", "linux-x86_64"):
            self.assertEqual(workflow.count(f"target: {target}"), 1)

    def test_release_attests_before_publishing(self) -> None:
        workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertLess(workflow.index("uses: actions/attest@v4"), workflow.index("gh release create"))
        self.assertIn("contents: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)

class ReadmeContractTests(unittest.TestCase):
    def test_primary_install_flow_is_standalone(self) -> None:
        readme = README.read_text(encoding="utf-8")
        self.assertIn(
            "curl -fsSL https://raw.githubusercontent.com/Manannikov-Nikita/hydra/main/install.sh | sh",
            readme,
        )
        self.assertIn("hydra-codex install -y", readme)
        self.assertIn("hydra-codex init .", readme)
        self.assertIn("hydra-codex status . --json", readme)
        self.assertIn("Developer installation", readme)
```

- [ ] **Step 2: Run tests and capture RED**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_workflow_contract \
  tests.test_readme_contract -v
```

Expected: FAIL because workflows and public standalone documentation do not exist.

- [ ] **Step 3: Implement Quality and release workflows**

Quality matrix:

```yaml
include:
  - runner: macos-15
    target: darwin-arm64
  - runner: macos-15-intel
    target: darwin-x86_64
  - runner: ubuntu-24.04
    target: linux-x86_64
```

Each job runs source tests, wheel/sdist inventory, native PyInstaller build, and `packaging/accept_standalone.sh`. Default permissions are `contents: read`.

Release workflow:

```text
1. Trigger only on v* tag pushes.
2. Verify clean exact tag and tag == "v" + hydra_codex.__version__.
3. Rerun source suite.
4. Build and accept one native target per matrix job.
5. Aggregate exactly three expected archives and reject extra files.
6. Generate sorted SHA256SUMS.
7. Attest archives through subject-checksums and attest SHA256SUMS.
8. Use the authenticated GitHub CLI to create a draft release, attach all assets without overwrite, then publish it; do not add a third-party release action.
```

Use the current GitHub runner and attestation contracts:

- https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- https://docs.github.com/en/enterprise-cloud%40latest/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations

- [ ] **Step 4: Write exact public operator and user instructions**

README starts with:

```bash
curl -fsSL https://raw.githubusercontent.com/Manannikov-Nikita/hydra/main/install.sh | sh
hydra-codex install -y
cd /path/to/project
hydra-codex init .
hydra-codex status . --json
hydra-codex dashboard
```

Document:

```bash
hydra-codex upgrade --check
hydra-codex upgrade
hydra-codex uninstall --keep-cli
hydra-codex uninstall
gh attestation verify \
  hydra-codex-0.1.0-darwin-arm64.tar.gz \
  --repo Manannikov-Nikita/hydra
```

Privacy documentation states exactly what persists, what never persists by default, platform database paths, and that uninstall preserves telemetry. Troubleshooting covers PATH, unsupported platform, Codex missing/incompatible, integration ownership conflict, checksum failure, read-only status, and dashboard port conflicts.

- [ ] **Step 5: Run source, workflow, docs, and build gates**

Run:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest \
  tests.test_workflow_contract \
  tests.test_readme_contract -v
env PYTHONPATH="$PWD/src" python3.12 -m unittest discover -s tests -t .
python3.12 -m build --no-isolation
git diff --check
```

Expected: all tests PASS; documentation commands match CLI help and workflow artifact names exactly.

- [ ] **Step 6: Commit**

```bash
git add .github README.md docs plugins/hydra-codex/README.md \
  tests/test_workflow_contract.py tests/test_readme_contract.py
git commit -m "ci(release): publish attested standalone bundles"
```

---

### Task 9: Exact-Head Review, Public Repository, and First Release Canary

**Files:**
- Modify only when review finds a demonstrated defect in files owned by Tasks 1–8.
- Verify: entire repository, GitHub repository settings, release assets, and a fresh external project.

**Interfaces:**
- Consumes: exact tested commit, authenticated GitHub CLI, workflows, release runbook.
- Produces: public repository, terminal-green Quality, `v0.1.0`, three attested assets, verified raw installer URL, and a clean-machine receipt.

- [ ] **Step 1: Run the complete local verification on an unchanged HEAD**

Run sequentially:

```bash
env PYTHONPATH="$PWD/src" python3.12 -m unittest discover -s tests -t .
python3.12 -m build --no-isolation
python3.12 -m PyInstaller --noconfirm --clean packaging/hydra-codex.spec
python3.12 packaging/build_standalone.py --output "$PWD/release"
sh packaging/accept_standalone.sh "$PWD"/release/hydra-codex-*.tar.gz
git diff --check
git status --short
```

Expected: all gates PASS, no test is skipped because of product behavior, and the worktree is clean except the deliberate local `release/` output ignored by Git.

- [ ] **Step 2: Obtain an independent exact-HEAD review**

Review must check:

```text
archive traversal and link handling
checksum-to-filename binding
atomic symlink ownership and rollback
Codex integration ownership and rollback
project init concurrency and protected paths
telemetry preservation on uninstall
frozen runtime without Python/PATH dependence
workflow permissions and tag/version binding
private path and prompt redaction
```

Expected: no unresolved Critical or Important findings. Every accepted finding starts with a failing regression test and repeats the relevant full gate.

- [ ] **Step 3: Authenticate and create the public repository**

Run only after `gh auth status` is healthy:

```bash
gh repo create Manannikov-Nikita/hydra \
  --public \
  --source . \
  --remote origin \
  --push
gh repo view Manannikov-Nikita/hydra --json nameWithOwner,visibility,defaultBranchRef,url
```

Expected: visibility is `PUBLIC`, default branch is `main`, and the pushed SHA equals local `HEAD`.

- [ ] **Step 4: Require terminal-green Quality on exact main**

Run:

```bash
gh run list --repo Manannikov-Nikita/hydra --workflow quality.yml --limit 1
gh run watch --repo Manannikov-Nikita/hydra --exit-status
```

Expected: all three native target jobs and source gates are terminal-green on exact `main`.

- [ ] **Step 5: Create and verify the first immutable release**

Run:

```bash
git tag -a v0.1.0 -m "Hydra v0.1.0"
git push origin v0.1.0
gh run watch --repo Manannikov-Nikita/hydra --exit-status
gh release view v0.1.0 --repo Manannikov-Nikita/hydra
```

Expected assets:

```text
hydra-codex-0.1.0-darwin-arm64.tar.gz
hydra-codex-0.1.0-darwin-x86_64.tar.gz
hydra-codex-0.1.0-linux-x86_64.tar.gz
SHA256SUMS
```

Verify each archive:

```bash
gh attestation verify \
  hydra-codex-0.1.0-darwin-arm64.tar.gz \
  --repo Manannikov-Nikita/hydra
```

- [ ] **Step 6: Run the public installer canary outside the checkout**

Run on a fresh supported user account or clean VM:

```bash
curl -fsSL https://raw.githubusercontent.com/Manannikov-Nikita/hydra/main/install.sh | sh
hydra-codex install -y
mkdir hydra-canary && cd hydra-canary && git init
hydra-codex init .
hydra-codex status . --json
hydra-codex dashboard
```

Expected: no Python dependency, plugin active, only `.hydra/project.toml` added to the project, status valid, dashboard reachable on loopback, and a test Codex task appears after refresh.

- [ ] **Step 7: Record the release receipt**

Create a GitHub release note section containing:

```text
exact source SHA
Quality run URL
release workflow URL
three archive SHA-256 values
attestation verification result
macOS arm64 canary result
macOS x64 canary result
Linux x64 canary result
Codex plugin/hook/MCP result
upgrade rollback result
uninstall data-preservation result
known limitations
```

Expected: all supported targets have evidence before announcing the installer as generally available.
