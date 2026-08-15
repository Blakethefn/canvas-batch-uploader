from __future__ import annotations

import unittest

from app.canvas_client import CanvasSecurityError, next_page_url


class PaginationLinkTests(unittest.TestCase):
    def test_returns_same_host_next_link(self) -> None:
        header = (
            '<https://school.example.edu/api/v1/courses?page=1>; rel="current", '
            '<https://school.example.edu/api/v1/courses?page=2>; rel="next"'
        )
        self.assertEqual(
            next_page_url(header, "https://school.example.edu"),
            "https://school.example.edu/api/v1/courses?page=2",
        )

    def test_rejects_cross_host_next_link(self) -> None:
        header = '<https://attacker.example/api/v1/courses?page=2>; rel="next"'
        with self.assertRaises(CanvasSecurityError):
            next_page_url(header, "https://school.example.edu")

    def test_returns_none_without_next_relation(self) -> None:
        header = '<https://school.example.edu/api/v1/courses?page=1>; rel="current"'
        self.assertIsNone(next_page_url(header, "https://school.example.edu"))


if __name__ == "__main__":
    unittest.main()
