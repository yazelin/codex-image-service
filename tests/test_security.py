import time
import unittest

from app.security import (
    constant_equals,
    create_admin_session,
    generate_api_key,
    hash_api_key,
    verify_admin_session,
)


class SecurityTests(unittest.TestCase):
    def test_api_key_is_prefixed_and_hash_is_stable(self):
        api_key = generate_api_key()

        self.assertTrue(api_key.startswith("cimg_"))
        self.assertEqual(hash_api_key(api_key), hash_api_key(api_key))
        self.assertNotEqual(hash_api_key(api_key), api_key)

    def test_admin_session_round_trip(self):
        token = create_admin_session("admin", "secret", ttl_seconds=60)

        self.assertEqual(verify_admin_session(token, "secret"), "admin")
        self.assertIsNone(verify_admin_session(token, "different-secret"))

    def test_expired_admin_session_is_rejected(self):
        token = create_admin_session("admin", "secret", ttl_seconds=-1)

        time.sleep(0.01)
        self.assertIsNone(verify_admin_session(token, "secret"))

    def test_constant_equals(self):
        self.assertTrue(constant_equals("same", "same"))
        self.assertFalse(constant_equals("same", "different"))


if __name__ == "__main__":
    unittest.main()

