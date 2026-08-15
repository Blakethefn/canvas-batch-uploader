from __future__ import annotations

import unittest

from app.config import ConfigurationError, url_has_same_origin, validate_canvas_base_url


class CanvasUrlValidationTests(unittest.TestCase):
    def test_normalizes_valid_https_url(self) -> None:
        self.assertEqual(
            validate_canvas_base_url("https://SCHOOL.example.edu/"),
            "https://school.example.edu",
        )

    def test_rejects_non_https_url(self) -> None:
        with self.assertRaises(ConfigurationError):
            validate_canvas_base_url("http://school.example.edu")

    def test_rejects_credentials_query_and_fragment(self) -> None:
        unsafe_values = (
            "https://person:secret@school.example.edu",
            "https://school.example.edu?token=secret",
            "https://school.example.edu/#settings",
        )
        for value in unsafe_values:
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                validate_canvas_base_url(value)

    def test_origin_comparison_honors_host_and_effective_port(self) -> None:
        base = "https://school.example.edu"
        self.assertTrue(url_has_same_origin("https://SCHOOL.example.edu:443/api/v1", base))
        self.assertFalse(url_has_same_origin("https://files.example.edu/api/v1", base))
        self.assertFalse(url_has_same_origin("https://school.example.edu:444/api/v1", base))


if __name__ == "__main__":
    unittest.main()
