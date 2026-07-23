from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import hydra_codex.project_config as project_config
from hydra_codex.project_config import (
    PROJECT_CONFIG_SCHEMA_VERSION,
    PROJECT_ID_PATTERN,
    ProjectConfig,
    ProjectConfigError,
    generate_project_id,
    parse_project_config,
    read_project_config,
    render_project_config,
)


class ProjectConfigTests(unittest.TestCase):
    def test_generated_identity_is_canonical(self) -> None:
        requested: list[int] = []

        project_id = generate_project_id(
            lambda size: requested.append(size) or b"\xab" * size,
        )

        self.assertEqual(requested, [8])
        self.assertEqual(project_id, "hprj_abababababababab")
        self.assertRegex(project_id, PROJECT_ID_PATTERN)

    def test_unknown_fields_fail_closed(self) -> None:
        with self.assertRaises(ProjectConfigError):
            parse_project_config(
                b'project_id = "hprj_0123456789abcdef"\nunknown = true\n',
                source=Path("project.toml"),
            )

    def test_current_and_canonical_legacy_files_are_readable(self) -> None:
        current = parse_project_config(
            b'schema_version = 1\nproject_id = "hprj_0123456789abcdef"\n'
            b'display_name = "Hydra Core"\ntelemetry = "hybrid"\n',
            source=Path("project.toml"),
        )
        legacy = parse_project_config(
            b'project_id = "hprj_fedcba9876543210"\ntelemetry = "hybrid"\n',
            source=Path("project.toml"),
        )

        self.assertEqual(current.schema_version, PROJECT_CONFIG_SCHEMA_VERSION)
        self.assertEqual(current.display_name, "Hydra Core")
        self.assertIsNone(legacy.schema_version)
        self.assertEqual(legacy.project_id, "hprj_fedcba9876543210")

    def test_invalid_shape_types_schema_identity_and_telemetry_fail_closed(self) -> None:
        invalid = (
            b"",
            b"project_id = 12\n",
            b'project_id = "project-a"\n',
            b'project_id = "hprj_ABCDEF0123456789"\n',
            b'schema_version = true\nproject_id = "hprj_0123456789abcdef"\n',
            b'schema_version = 2\nproject_id = "hprj_0123456789abcdef"\n',
            b'project_id = "hprj_0123456789abcdef"\ndisplay_name = 4\n',
            b'project_id = "hprj_0123456789abcdef"\ntelemetry = "remote"\n',
            b'[project]\nproject_id = "hprj_0123456789abcdef"\n',
            b"\xff",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ProjectConfigError):
                parse_project_config(raw, source=Path("/private/work/project.toml"))

    def test_parse_errors_do_not_disclose_absolute_source_paths(self) -> None:
        source = Path("/private/work/secret-project/.hydra/project.toml")

        with self.assertRaises(ProjectConfigError) as raised:
            parse_project_config(b"not = [valid", source=source)

        self.assertNotIn(str(source), str(raised.exception))
        self.assertNotIn("secret-project", str(raised.exception))

    def test_render_is_deterministic_private_and_round_trips(self) -> None:
        config = ProjectConfig(
            schema_version=PROJECT_CONFIG_SCHEMA_VERSION,
            project_id="hprj_0123456789abcdef",
            display_name='Caf\u00e9 "Core"',
            telemetry="hybrid",
        )

        rendered = render_project_config(config)

        self.assertEqual(
            rendered,
            b'schema_version = 1\n'
            b'project_id = "hprj_0123456789abcdef"\n'
            b'display_name = "Caf\\u00e9 \\"Core\\""\n'
            b'telemetry = "hybrid"\n',
        )
        self.assertEqual(
            parse_project_config(rendered, source=Path("project.toml")),
            config,
        )

    def test_render_rejects_invalid_programmatic_config(self) -> None:
        with self.assertRaises(ProjectConfigError):
            render_project_config(ProjectConfig(1, "not-canonical", None, "hybrid"))

    def test_streamed_reader_rejects_content_above_exported_size_cap(self) -> None:
        self.assertEqual(project_config.PROJECT_CONFIG_MAX_BYTES, 64 * 1024)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private-project-name.toml"
            path.write_bytes(
                b'project_id = "hprj_0123456789abcdef"\n'
                + b"#" * project_config.PROJECT_CONFIG_MAX_BYTES,
            )

            with self.assertRaises(ProjectConfigError) as raised:
                read_project_config(path)

        self.assertNotIn(str(path), str(raised.exception))
        self.assertNotIn("private-project-name", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
