import unittest

from src.realm_protector.services.request_limits import Cooldown


class CooldownTests(unittest.TestCase):
    def test_claim_blocks_only_the_same_key_until_deadline(self) -> None:
        cooldown = Cooldown(30)

        self.assertEqual(0.0, cooldown.claim((1, 2), now=100.0))
        self.assertEqual(25.0, cooldown.claim((1, 2), now=105.0))
        self.assertEqual(0.0, cooldown.claim((1, 3), now=105.0))
        self.assertEqual(0.0, cooldown.claim((1, 2), now=130.0))

    def test_invalid_limits_fail_fast(self) -> None:
        with self.assertRaises(ValueError):
            Cooldown(0)
        with self.assertRaises(ValueError):
            Cooldown(1, max_entries=0)


if __name__ == "__main__":
    unittest.main()
