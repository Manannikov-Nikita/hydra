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

The Linux job also downloads the official ShellCheck 0.11.0 archive, verifies
its pinned SHA-256 digest, and checks `install.sh` plus
`packaging/accept_standalone.sh`. Workflow actions are pinned to reviewed commit
SHAs. Quality has read-only repository permission and receives no release
credentials.

## Tag contract

Create a tag only after exact-head review and green Quality. A release tag is
strictly `vMAJOR.MINOR.PATCH`, and its version must equal
`hydra_codex.__version__`. The workflow peels both lightweight and annotated
tags to a commit, requires that commit to equal the checkout, and rejects a
dirty checkout.

Each stable version must be strictly newer than the repository's current latest
stable release. The workflow refuses older tags and backfilled versions rather
than moving the installer's `/releases/latest` pointer backward.

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

After downloading an archive and `SHA256SUMS`, first verify every manifest
entry. On Linux:

```bash
sha256sum -c SHA256SUMS
```

On macOS use `shasum -a 256 -c SHA256SUMS`. Then verify both the manifest
attestation and the archive attestation:

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
