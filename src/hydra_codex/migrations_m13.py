"""Private filesystem identity cache for unchanged rollout locations."""

from __future__ import annotations


M13_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (29, (
        """CREATE TABLE rollout_source_location_states (
               project_id TEXT NOT NULL,
               location_key TEXT NOT NULL,
               logical_source_key TEXT NOT NULL
                   REFERENCES rollout_logical_sources(logical_source_key),
               revision_digest TEXT NOT NULL
                   REFERENCES rollout_sources(source_digest),
               st_dev INTEGER NOT NULL,
               st_ino INTEGER NOT NULL,
               st_size INTEGER NOT NULL,
               st_mtime_ns INTEGER NOT NULL,
               st_ctime_ns INTEGER NOT NULL,
               scanner_version INTEGER NOT NULL,
               PRIMARY KEY(project_id,location_key),
               FOREIGN KEY(logical_source_key,location_key)
                   REFERENCES rollout_source_locations(logical_source_key,location_key)
           )""",
    )),
)


M13_REQUIRED_SCHEMA = {
    "rollout_source_location_states": {
        "project_id", "location_key", "logical_source_key", "revision_digest",
        "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns",
        "scanner_version",
    },
}
