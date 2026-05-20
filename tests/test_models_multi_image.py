"""Pydantic-level checks for the multi-image request shape."""

from __future__ import annotations

import unittest

from app.models import ImageGenerateRequest


class MultiImageRequestModel(unittest.TestCase):
    def test_singular_field_still_accepted(self):
        req = ImageGenerateRequest(prompt="x", reference_image_base64="AAA")
        self.assertEqual(req.resolved_reference_images, ["AAA"])

    def test_plural_field_accepted(self):
        req = ImageGenerateRequest(
            prompt="x",
            reference_images_base64=["AAA", "BBB"],
        )
        self.assertEqual(req.resolved_reference_images, ["AAA", "BBB"])

    def test_plural_takes_precedence_when_both_set(self):
        req = ImageGenerateRequest(
            prompt="x",
            reference_image_base64="OLD",
            reference_images_base64=["NEW1", "NEW2"],
        )
        self.assertEqual(req.resolved_reference_images, ["NEW1", "NEW2"])

    def test_neither_field_returns_empty_list(self):
        req = ImageGenerateRequest(prompt="x")
        self.assertEqual(req.resolved_reference_images, [])


if __name__ == "__main__":
    unittest.main()
