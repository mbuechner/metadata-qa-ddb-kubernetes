from __future__ import annotations

import unittest

from metadata_qa.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_are_bounded_and_same_origin(self) -> None:
        settings = Settings.from_env({})

        self.assertEqual(settings.namespace, "ddbmetadata-qa")
        self.assertEqual(settings.default_log_tail_lines, 2_000)
        self.assertEqual(settings.max_log_tail_lines, 5_000)
        self.assertIsNone(settings.cors_allowed_origins)
        self.assertEqual(settings.url_prefix, "")
        self.assertFalse(settings.httpauth_enabled)
        self.assertTrue(settings.secret_key_is_ephemeral)

    def test_partial_basic_auth_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "must either both be set"):
            Settings.from_env({"HTTPAUTH_USERNAME": "operator"})

    def test_invalid_integer_has_actionable_error(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "PORT must be an integer"):
            Settings.from_env({"PORT": "not-a-number"})

    def test_default_tail_must_not_exceed_maximum(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "DEFAULT_LOG_TAIL_LINES must be between"):
            Settings.from_env({"MAX_LOG_TAIL_LINES": "10", "DEFAULT_LOG_TAIL_LINES": "11"})

    def test_cronjob_name_reserves_space_for_generated_job_suffix(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "maximum 52"):
            Settings.from_env({"CRONJOB_NAME": "a" * 53})

    def test_job_name_candidate_requires_exact_legacy_shape(self) -> None:
        settings = Settings.from_env({"CRONJOB_NAME": "nightly"})

        self.assertTrue(settings.is_managed_job_name_candidate("nightly-1700000000"))
        self.assertFalse(settings.is_managed_job_name_candidate("nightly-admin"))
        self.assertFalse(settings.is_managed_job_name_candidate("other-1700000000"))

    def test_explicit_cors_origins_are_normalized(self) -> None:
        settings = Settings.from_env(
            {"CORS_ALLOWED_ORIGINS": " https://one.example,https://two.example "}
        )

        self.assertEqual(
            settings.cors_allowed_origins,
            ("https://one.example", "https://two.example"),
        )

    def test_wildcard_cors_remains_an_explicit_compatibility_option(self) -> None:
        settings = Settings.from_env({"CORS_ALLOWED_ORIGINS": "*"})

        self.assertEqual(settings.cors_allowed_origins, "*")

    def test_invalid_realm_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "HTTPAUTH_REALM"):
            Settings.from_env({"HTTPAUTH_REALM": 'bad"realm'})

    def test_invalid_log_level_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "LOG_LEVEL"):
            Settings.from_env({"LOG_LEVEL": "verbose"})

    def test_url_prefix_is_normalized(self) -> None:
        settings = Settings.from_env({"URL_PREFIX": " /app/manager/ "})

        self.assertEqual(settings.url_prefix, "/app/manager")

    def test_invalid_url_prefix_is_rejected(self) -> None:
        invalid_values = ["app/manager", "/app//manager", "/app/../manager", "/app%2Fmanager"]

        for value in invalid_values:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ConfigurationError, "URL_PREFIX"),
            ):
                Settings.from_env({"URL_PREFIX": value})


if __name__ == "__main__":
    unittest.main()
