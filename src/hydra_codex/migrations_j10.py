"""Quarantine legacy token vectors that violate deterministic invariants."""

from __future__ import annotations


J10_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (26, (
        """ALTER TABLE token_snapshots
               ADD COLUMN vector_valid INTEGER NOT NULL DEFAULT 1
               CHECK(vector_valid IN (0,1))""",
        """ALTER TABLE fork_baselines
               ADD COLUMN vector_valid INTEGER NOT NULL DEFAULT 1
               CHECK(vector_valid IN (0,1))""",
        "ALTER TABLE fork_baselines ADD COLUMN validation_caveat TEXT",
        """UPDATE token_snapshots
              SET vector_valid=0,contributes_total=0,
                  selection_provenance='estimated',
                  selection_caveat='invalid_legacy_token_vector'
            WHERE (input_tokens IS NOT NULL AND input_tokens<0)
               OR (cached_input_tokens IS NOT NULL AND cached_input_tokens<0)
               OR (output_tokens IS NOT NULL AND output_tokens<0)
               OR (reasoning_tokens IS NOT NULL AND reasoning_tokens<0)
               OR (cache_write_tokens IS NOT NULL AND cache_write_tokens<0)
               OR (input_tokens IS NOT NULL AND cached_input_tokens IS NOT NULL
                   AND cached_input_tokens>input_tokens)""",
        """UPDATE fork_baselines
              SET vector_valid=0,
                  validation_caveat='invalid_legacy_token_vector'
            WHERE input_tokens<0 OR cached_input_tokens<0 OR output_tokens<0
               OR reasoning_tokens<0 OR cache_write_tokens<0
               OR cached_input_tokens>input_tokens""",
    )),
)


J10_REQUIRED_SCHEMA = {
    "token_snapshots": {"vector_valid"},
    "fork_baselines": {"vector_valid", "validation_caveat"},
}
