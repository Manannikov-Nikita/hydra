"""Pure collision-expanding projections for privacy-safe public task refs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from types import MappingProxyType
from typing import Iterable, Mapping


@dataclass(frozen=True, repr=False)
class PublicReferenceProjection:
    """Private lookup with a repr that cannot disclose its opaque input IDs."""

    _by_private_id: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_by_private_id",
            MappingProxyType(dict(sorted(self._by_private_id.items()))),
        )

    def __getitem__(self, private_id: str) -> str:
        try:
            return self._by_private_id[private_id]
        except KeyError:
            raise KeyError("unknown opaque identifier") from None

    @property
    def public_references(self) -> tuple[str, ...]:
        return tuple(self._by_private_id.values())

    def __len__(self) -> int:
        return len(self._by_private_id)

    def __repr__(self) -> str:
        return f"PublicReferenceProjection(count={len(self)})"


def project_public_references(
    opaque_ids: Iterable[str], installation_key: bytes, *, minimum_length: int = 12,
) -> PublicReferenceProjection:
    """Project private opaque IDs to stable refs, expanding only colliding prefixes."""
    if not isinstance(installation_key, bytes) or len(installation_key) < 16:
        raise ValueError("installation key must contain at least 16 bytes")
    if isinstance(minimum_length, bool) or not isinstance(minimum_length, int) or not 1 <= minimum_length <= 64:
        raise ValueError("minimum_length must be between 1 and 64")
    supplied = tuple(opaque_ids)
    if any(not isinstance(item, str) or not item for item in supplied):
        raise ValueError("opaque IDs must be non-empty text")
    identifiers = tuple(sorted(set(supplied)))
    digests = {
        item: hmac.new(
            installation_key, b"hydra/public-task-ref/v1/" + item.encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        for item in identifiers
    }
    lengths = {item: minimum_length for item in identifiers}
    while True:
        groups: dict[str, list[str]] = {}
        for item in identifiers:
            groups.setdefault(digests[item][:lengths[item]], []).append(item)
        collisions = tuple(group for group in groups.values() if len(group) > 1)
        if not collisions:
            break
        for group in collisions:
            if any(lengths[item] == 64 for item in group):
                raise ValueError("public reference digest collision")
            for item in group:
                lengths[item] += 1
    return PublicReferenceProjection({
        item: f"task_{digests[item][:lengths[item]]}" for item in identifiers
    })
