import concurrent.futures
from pathlib import Path
import tempfile
import unittest

import database as db
import psx_shop


class PsxShopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp_dir.name) / "tap_simulator.db"
        db.init_db()
        psx_shop.ensure_catalog()

    def tearDown(self) -> None:
        db.DB_PATH = self.previous_db_path
        self.temp_dir.cleanup()

    def test_catalog_and_name_resolution(self) -> None:
        self.assertEqual(len(psx_shop.catalog_state()), 3)
        self.assertEqual(
            psx_shop.resolve_pet("Golden Huge Hell Rock").pet_id,
            "golden_huge_hell_rock",
        )
        self.assertEqual(psx_shop.resolve_pet("huge_capcake").price_shards, 1500)
        self.assertIsNone(psx_shop.resolve_pet("not a PSX pet"))

    def test_purchase_marks_pet_global_and_delivers_receipt(self) -> None:
        db.add_shards(100, 1000)
        purchase = psx_shop.purchase_pet(
            100,
            "golden_huge_hell_rock",
            code_factory=lambda: "tap_bot-TEST123",
        )

        self.assertTrue(purchase.ok)
        self.assertEqual(purchase.code, "tap_bot-TEST123")
        self.assertEqual(db.shards_of(100), 0)
        self.assertTrue(db.psx_pet_locked("golden_huge_hell_rock"))
        self.assertTrue(db.psx_mark_dm_sent(purchase.purchase_id))
        self.assertEqual(db.psx_refund_pending(purchase.purchase_id), "completed")

        other = psx_shop.purchase_pet(101, "golden_huge_hell_rock")
        self.assertFalse(other.ok)
        self.assertEqual(other.reason, "already_claimed")

    def test_failed_dm_refunds_and_releases_pet(self) -> None:
        db.add_shards(200, 1500)
        purchase = psx_shop.purchase_pet(
            200,
            "huge_capcake",
            code_factory=lambda: "tap_bot-RETRY1",
        )
        self.assertTrue(purchase.ok)
        self.assertEqual(psx_shop.refund_if_dm_failed(purchase.purchase_id), "refunded")
        self.assertEqual(db.shards_of(200), 1500)
        self.assertFalse(db.psx_pet_locked("huge_capcake"))

        retry = psx_shop.purchase_pet(
            200,
            "huge_capcake",
            code_factory=lambda: "tap_bot-RETRY2",
        )
        self.assertTrue(retry.ok)
        self.assertEqual(retry.code, "tap_bot-RETRY2")
        self.assertEqual(db.shards_of(200), 0)

    def test_simultaneous_buyers_cannot_both_claim_one_copy(self) -> None:
        db.add_shards(301, 1000)
        db.add_shards(302, 1000)

        def buy(user_id: int):
            return psx_shop.purchase_pet(
                user_id,
                "golden_huge_hell_rock",
                code_factory=lambda: f"tap_bot-{user_id}",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(buy, (301, 302)))

        self.assertEqual(sum(result.ok for result in results), 1)
        self.assertEqual(
            sum(result.reason == "already_claimed" for result in results),
            1,
        )
        self.assertEqual(db.shards_of(301) + db.shards_of(302), 1000)


if __name__ == "__main__":
    unittest.main()
