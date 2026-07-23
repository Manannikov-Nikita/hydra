# Release process

This runbook describes the public standalone release contract. Release
publication is automated from a clean, reviewed tag; it is not a developer
shortcut around Quality.

## Quality

Every push and pull request runs the full source suite, wheel/source inventory,
native PyInstaller build, and standalone acceptance on exactly:

- `macos-15` as `darwin-arm64`;
- `macos-15-intel` as `darwin-x86_64`;
- `ubuntu-24.04` as `linux-x86_64`.

Those runners define the first release's runtime compatibility contract:
macOS 15 on each native architecture, and Ubuntu 24.04 x86-64 with glibc 2.39.
Do not describe an older OS or libc as supported until the exact released
archive has passed a clean-system canary there. PyInstaller does not make an
architecture-only Linux compatibility guarantee by bundling glibc.

The Linux job also downloads the official ShellCheck 0.11.0 archive, verifies
its pinned SHA-256 digest, and checks `install.sh` plus
`packaging/accept_standalone.sh`. Workflow actions are pinned to reviewed commit
SHAs. Quality has read-only repository permission and receives no release
credentials.

Release tooling is installed only as wheels from
`requirements/release-tools.txt`, with exact versions and reviewed SHA-256
digests under pip's `--require-hashes` mode. Builds use `--no-isolation`; a
release-tool update therefore requires an explicit lock review rather than
resolving a new PyInstaller hook, setuptools, or build backend during CI.

## Tag contract

Create a tag only after exact-head review and green Quality. A release tag is
strictly `vMAJOR.MINOR.PATCH`, and its version must equal
`hydra_codex.__version__`. The workflow peels both lightweight and annotated
tags to a commit, requires that commit to equal the checkout, and rejects a
dirty checkout.

Each stable version must be strictly newer than the repository's current latest
stable release. The workflow refuses older tags and backfilled versions rather
than moving the installer's `/releases/latest` pointer backward.
All release tags share one non-cancelling concurrency group, so two versions
cannot pass the latest-release guard and publish out of order.

The tag workflow rebuilds and accepts one native archive per matrix runner. The
publish job downloads each matrix artifact into a separate directory and
requires exactly these three regular, non-symlink files:

```text
hydra-codex-VERSION-darwin-arm64.tar.gz
hydra-codex-VERSION-darwin-x86_64.tar.gz
hydra-codex-VERSION-linux-x86_64.tar.gz
```

It generates one lexically sorted `SHA256SUMS`. The archives are attested from
that manifest, and `SHA256SUMS` receives its own GitHub artifact attestation.
Only the final publish job receives repository, OIDC, attestation, and artifact
metadata write permissions.

## Fail-closed publication

Publication creates a draft with `gh release create --draft --verify-tag`.
A workflow retry may continue an existing draft only when its tag and every
existing asset byte-for-byte match the local accepted outputs. Missing exact
assets are uploaded without `--clobber`. Extra names, duplicate names, changed
digests, or a published release stop the workflow.

Immediately before publication, the workflow downloads all four remote assets,
rechecks their exact inventory and digests, and then changes the draft to
published. The workflow treats an existing published release as immutable: do
not replace an asset, move the tag, republish the version, or overwrite it
manually. Correct a release defect with a new version.

Before creating the first public release tag, Task 9 must enable and then read
back GitHub's repository-level immutable-release setting:

```bash
gh api --method PUT repos/Manannikov-Nikita/hydra/immutable-releases
gh api --method GET repos/Manannikov-Nikita/hydra/immutable-releases
```

Do not describe the repository or its release assets as immutable until that
read-back succeeds. The workflow's fail-closed policy prevents its own
overwrite path, but does not by itself turn that repository setting on.

## User verification

After downloading one archive and `SHA256SUMS`, bind verification to exactly
that archive's manifest row. For example:

```bash
archive=hydra-codex-0.1.0-darwin-arm64.tar.gz
awk -v file="$archive" '$2 == file {print}' SHA256SUMS > SHA256SUMS.target
[ "$(wc -l < SHA256SUMS.target | tr -d '[:space:]')" = 1 ]
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c SHA256SUMS.target
else
  shasum -a 256 -c SHA256SUMS.target
fi
```

Do not run the unfiltered three-archive manifest after downloading only one
archive: the two absent files correctly make that command fail. Then verify
both the manifest attestation and the archive attestation:

```bash
gh attestation verify \
  SHA256SUMS \
  --repo Manannikov-Nikita/hydra

gh attestation verify \
  hydra-codex-0.1.0-darwin-arm64.tar.gz \
  --repo Manannikov-Nikita/hydra
```

Checksums prove that two byte sequences match. The GitHub artifact attestation
binds the archive to the repository workflow identity. The initial convenient
installer URL uses mutable `main`; review or commit-pin that bootstrap when
immutable provenance is required from the first byte.
