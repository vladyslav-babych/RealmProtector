import importlib
import sys
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch


class MainEntrypointTests(unittest.TestCase):
    def test_runtime_version_matches_package_metadata(self) -> None:
        from src.realm_protector import __version__

        project = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(project["project"]["version"], __version__)

    def test_unsupported_python_fails_before_application_imports(self) -> None:
        from src.realm_protector import validate_python_version

        with self.assertRaisesRegex(SystemExit, "requires Python 3.12"):
            validate_python_version((3, 9, 6))

        validate_python_version((3, 12, 0))
        validate_python_version((3, 14, 99))

    def test_importing_main_never_starts_a_discord_connection(self) -> None:
        previous_main = sys.modules.pop("main", None)
        try:
            with patch("discord.ext.commands.Bot.run") as run:
                module = importlib.import_module("main")

            run.assert_not_called()
            self.assertTrue(callable(module.main))
        finally:
            sys.modules.pop("main", None)
            if previous_main is not None:
                sys.modules["main"] = previous_main


class TokenSelectionTests(unittest.TestCase):
    @staticmethod
    def _resolve(values: dict[str, str]) -> str:
        from main import resolve_discord_token

        return resolve_discord_token(values)

    def test_production_environment_uses_only_the_production_token(self) -> None:
        self.assertEqual(
            "production-secret",
            self._resolve(
                {
                    "BOT_ENV": " Production ",
                    "DISCORD_TOKEN": " production-secret ",
                    "DISCORD_TOKEN_TEST": "test-secret",
                }
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "Missing DISCORD_TOKEN"):
            self._resolve(
                {
                    "BOT_ENV": "production",
                    "DISCORD_TOKEN_TEST": "test-secret",
                }
            )

    def test_non_production_environment_uses_only_the_test_token(self) -> None:
        self.assertEqual(
            "test-secret",
            self._resolve(
                {
                    "BOT_ENV": "development",
                    "DISCORD_TOKEN": "production-secret",
                    "DISCORD_TOKEN_TEST": " test-secret ",
                }
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "Missing DISCORD_TOKEN_TEST"):
            self._resolve(
                {
                    "BOT_ENV": "test",
                    "DISCORD_TOKEN": "production-secret",
                }
            )

    def test_unspecified_environment_preserves_production_startup(self) -> None:
        self.assertEqual(
            "production-secret",
            self._resolve({"DISCORD_TOKEN": "production-secret"}),
        )
        self.assertEqual(
            "production-secret",
            self._resolve(
                {
                    "DISCORD_TOKEN_TEST": "test-secret",
                    "DISCORD_TOKEN": "production-secret",
                }
            ),
        )

    def test_unknown_environment_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Unsupported BOT_ENV"):
            self._resolve(
                {
                    "BOT_ENV": "staging",
                    "DISCORD_TOKEN": "production-secret",
                    "DISCORD_TOKEN_TEST": "test-secret",
                }
            )

    def test_missing_or_blank_tokens_fail_fast(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Missing DISCORD_TOKEN"):
            self._resolve({"DISCORD_TOKEN_TEST": "   ", "DISCORD_TOKEN": ""})


if __name__ == "__main__":
    unittest.main()
