# Install Hydra

The public standalone installer supports macOS on Apple Silicon and Intel, and
Linux on x86-64. The installed runtime is per-user and does not require Python.

## Public installation

Run the bootstrap, connect the bundled Hydra plugin to Codex, and initialize
each project that should contribute telemetry:

```bash
curl -fsSL https://raw.githubusercontent.com/Manannikov-Nikita/hydra/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
hydra-codex install -y
cd /path/to/project
hydra-codex init .
hydra-codex status . --json
hydra-codex dashboard
```

`hydra-codex install -y` is the non-interactive form. Without `-y`, Hydra asks
before changing the user's Codex plugin configuration. Start a new Codex task
after installation so the host loads the new hooks.

`status` is read-only. It verifies project initialization, local runtime state,
and Codex integration without importing rollouts or opening the database for
mutation.

The dashboard binds only to loopback. It opens a browser by default and accepts
only explicit flags, not a positional project:

```bash
hydra-codex dashboard --cwd /path/to/project --port 0 --no-open
```

`--cwd` selects the project, `--port 0` asks the operating system for a free
loopback port, and `--no-open` prints the URL instead of opening it.

## Bootstrap trust boundary

The convenient `raw.githubusercontent.com/.../main/install.sh` URL is mutable:
the repository owner can change what a later invocation downloads. Read the
script before running it when that trust boundary is inappropriate. The script
downloads a versioned archive, verifies its entry in `SHA256SUMS`, validates the
archive layout, and activates only the verified bundle.

For a cryptographic identity tied to GitHub's release workflow, download the
versioned archive and verify its GitHub artifact attestation as described in
[the release process](release-process.md). A checksum detects corruption or a
mismatched download; by itself it does not establish who published the
checksum. Repository-level immutable-release enforcement is verified during
the first public release canary, not assumed from the bootstrap script.

## Developer installation

Contributors can run Hydra directly from a checkout:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test,release]'
env PYTHONPATH="$PWD/src" .venv/bin/python -m unittest discover -s tests -t .
```

Activate the environment before using the short command, or invoke
`.venv/bin/hydra-codex`. Developer installation does not exercise the frozen
standalone runtime and is not a substitute for standalone acceptance.

See also:

- [Upgrade and uninstall](upgrade-and-uninstall.md)
- [Privacy and retained data](privacy.md)
- [Troubleshooting](troubleshooting.md)
