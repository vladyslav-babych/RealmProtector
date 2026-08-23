import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.realm_protector.services import local_maintenance


class LocalMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def test_retention_is_bounded_and_uses_utc_cutoffs(self) -> None:
        now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
        with (
            patch.object(
                local_maintenance.local_repository,
                "prune_completed_outbox",
                side_effect=[1_000, 7],
            ) as prune_outbox,
            patch.object(
                local_maintenance.local_repository,
                "prune_applied_sheet_snapshots",
                return_value=4,
            ) as prune_snapshots,
        ):
            result = local_maintenance.run_local_retention(now=now)

        self.assertEqual(1_007, result.completed_outbox_events)
        self.assertEqual(4, result.applied_sheet_snapshots)
        self.assertEqual(2, prune_outbox.call_count)
        self.assertEqual(
            datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
            prune_outbox.call_args_list[0].args[0],
        )
        self.assertEqual(1_000, prune_outbox.call_args_list[0].kwargs["limit"])
        self.assertEqual(
            datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
            prune_snapshots.call_args.args[0],
        )

    async def test_worker_is_cancelled_and_reset_during_shutdown(self) -> None:
        await local_maintenance.stop_local_maintenance()
        started = asyncio.Event()

        async def worker():
            started.set()
            await asyncio.Event().wait()

        with patch.object(local_maintenance, "_maintenance_loop", side_effect=worker):
            local_maintenance.start_local_maintenance()
            await started.wait()
            task = local_maintenance._maintenance_task
            await local_maintenance.stop_local_maintenance()

        self.assertIsNotNone(task)
        self.assertTrue(task.done())
        self.assertIsNone(local_maintenance._maintenance_task)


if __name__ == "__main__":
    unittest.main()
