import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.realm_protector.bot import message_checkpoints, objectives


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        frozen = cls(2026, 8, 21, 12, 30, tzinfo=timezone.utc)
        return frozen if tz is not None else frozen.replace(tzinfo=None)


class ObjectiveTimeParsingTests(unittest.TestCase):
    def test_normalizes_time_and_uses_the_next_matching_utc_occurrence(self) -> None:
        with patch.object(objectives, "datetime", FrozenDateTime):
            parsed = objectives._parse_utc_hhmm("7:05")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        label, timestamp = parsed
        self.assertEqual("07:05", label)
        self.assertEqual(
            int(datetime(2026, 8, 22, 7, 5, tzinfo=timezone.utc).timestamp()),
            timestamp,
        )

    def test_same_minute_rolls_forward_to_tomorrow(self) -> None:
        with patch.object(objectives, "datetime", FrozenDateTime):
            parsed = objectives._parse_utc_hhmm("12:30")

        self.assertEqual(
            (
                "12:30",
                int(
                    datetime(
                        2026,
                        8,
                        22,
                        12,
                        30,
                        tzinfo=timezone.utc,
                    ).timestamp()
                ),
            ),
            parsed,
        )

    def test_rejects_invalid_hours_minutes_and_formats(self) -> None:
        for value in ("", "24:00", "23:60", "-1:30", "12", "12:3", "12:30 UTC"):
            with self.subTest(value=value):
                self.assertIsNone(objectives._parse_utc_hhmm(value))


class ObjectiveNotificationRoleTests(unittest.TestCase):
    def test_role_name_is_deterministic_for_the_same_objective(self) -> None:
        objective = {
            "id": "objective-a",
            "type": "Vortex",
            "rarity": "Epic",
            "pop_time_utc": "18:30",
            "map": "Arthur's Rest",
        }

        self.assertEqual(
            objectives._build_notify_role_name(objective),
            objectives._build_notify_role_name(dict(objective)),
        )
        self.assertEqual("Vortex-Epic-18:30", objectives._build_notify_role_name(objective))
        self.assertNotRegex(objectives._build_notify_role_name(objective), r"-[0-9a-f]{10}$")

    def test_transient_role_names_distinguish_identical_descriptions_invisibly(self) -> None:
        first = {
            "id": "objective-a",
            "type": "Vortex",
            "rarity": "Epic",
            "pop_time_utc": "18:30",
            "map": "Arthur's Rest",
        }
        second = {
            "id": "objective-b",
            "type": "Vortex",
            "rarity": "Epic",
            "pop_time_utc": "18:30",
            "map": "Merlyn's Rest",
        }

        first_name = objectives._build_notify_role_name(first)
        second_name = objectives._build_notify_role_name(second)
        first_pending = objectives._build_pending_notify_role_name(30, first)
        second_pending = objectives._build_pending_notify_role_name(30, second)

        self.assertEqual(first_name, second_name)
        self.assertNotEqual(first_pending, second_pending)
        self.assertTrue(first_pending.startswith(first_name))
        self.assertIn(
            message_checkpoints.hidden_checkpoint(
                objectives._notify_role_checkpoint_marker(30, first),
                bit_count=40,
            ),
            first_pending,
        )
        self.assertNotRegex(first_pending, r"-[0-9a-f]{10}$")
        self.assertLessEqual(len(first_name), 100)
        self.assertLessEqual(len(first_pending), 100)

    def test_node_role_name_is_clean_and_descriptive(self) -> None:
        objective = {
            "id": "node-a",
            "type": "Node",
            "node_type": "Ore",
            "tier": "8.4",
            "pop_time_utc": "09:15",
        }

        self.assertEqual("Ore-8.4-09:15", objectives._build_notify_role_name(objective))


class ObjectiveRoleOwnershipTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _objective() -> dict:
        objective = {
            "id": "objective-owned",
            "type": "Vortex",
            "rarity": "Epic",
            "pop_time_utc": "18:30",
            "notify_role_id": 20,
            "notify_role_created_by_bot": True,
        }
        objective["notify_role_name"] = objectives._build_notify_role_name(objective)
        return objective

    @staticmethod
    def _role(name: str):
        return SimpleNamespace(
            id=20,
            name=name,
            managed=False,
            permissions=SimpleNamespace(),
            is_default=lambda: False,
            is_assignable=lambda: True,
            delete=AsyncMock(),
            edit=AsyncMock(),
        )

    async def test_renamed_role_is_detached_but_never_deleted(self) -> None:
        objective = self._objective()
        role = self._role("Repurposed Officer Role")
        guild = SimpleNamespace(id=30, me=None, get_role=lambda role_id: role)

        with (
            patch.object(
                objectives,
                "_load_config",
                return_value={"30": {"objectives": [dict(objective)]}},
            ),
            patch.object(objectives.logging, "warning"),
        ):
            cleaned = await objectives._cleanup_objective_notification_assets(
                guild,
                objective,
                fallback_channel_id=None,
            )

        self.assertTrue(cleaned)
        role.delete.assert_not_awaited()
        self.assertNotIn("notify_role_id", objective)
        self.assertNotIn("notify_role_name", objective)
        self.assertNotIn("notify_role_created_by_bot", objective)

    async def test_exact_owned_role_can_be_deleted(self) -> None:
        objective = self._objective()
        role = self._role(objective["notify_role_name"])
        guild = SimpleNamespace(id=30, me=None, get_role=lambda role_id: role)
        security_state = objectives.role_security.RoleSecurityState(
            self_assignable_id_sources={20: frozenset({"objective notification objective-owned"})}
        )

        with (
            patch.object(
                objectives,
                "_load_config",
                return_value={"30": {"objectives": [dict(objective)]}},
            ),
            patch.object(
                objectives.role_security,
                "collect_role_security_state",
                return_value=security_state,
            ),
        ):
            cleaned = await objectives._cleanup_objective_notification_assets(
                guild,
                objective,
                fallback_channel_id=None,
            )

        self.assertTrue(cleaned)
        role.delete.assert_awaited_once()
        self.assertNotIn("notify_role_id", objective)

    async def test_legacy_hashed_role_name_is_cleaned_after_verified_id(self) -> None:
        objective = self._objective()
        legacy_name = objectives._build_legacy_notify_role_name(objective)
        objective["notify_role_name"] = legacy_name
        role = self._role(legacy_name)
        guild = SimpleNamespace(id=30, me=None, get_role=lambda role_id: role)
        security_state = objectives.role_security.RoleSecurityState(
            self_assignable_id_sources={20: frozenset({"objective notification objective-owned"})}
        )

        with patch.object(
            objectives.role_security,
            "collect_role_security_state",
            return_value=security_state,
        ):
            cleaned = await objectives._clean_persisted_notify_role_name(guild, objective)

        self.assertTrue(cleaned)
        role.edit.assert_awaited_once_with(
            name="Vortex-Epic-18:30",
            reason="Finalize objective notification role name",
        )
        self.assertEqual("Vortex-Epic-18:30", objective["notify_role_name"])
        self.assertTrue(objective[objectives._NOTIFY_ROLE_NAME_CLEANUP_FIELD])

    async def test_role_name_cleanup_failure_is_left_unflagged_and_retried(self) -> None:
        objective = self._objective()
        legacy_name = objectives._build_legacy_notify_role_name(objective)
        objective["notify_role_name"] = legacy_name
        role = self._role(legacy_name)
        role.edit.side_effect = [AttributeError("temporarily unavailable"), None]
        guild = SimpleNamespace(id=30, me=None, get_role=lambda role_id: role)

        with patch.object(objectives, "_notify_role_ownership_error", return_value=None):
            first_cleaned = await objectives._clean_persisted_notify_role_name(guild, objective)
            self.assertNotIn(objectives._NOTIFY_ROLE_NAME_CLEANUP_FIELD, objective)
            self.assertEqual(legacy_name, objective["notify_role_name"])
            second_cleaned = await objectives._clean_persisted_notify_role_name(guild, objective)

        self.assertFalse(first_cleaned)
        self.assertTrue(second_cleaned)
        self.assertEqual(2, role.edit.await_count)
        self.assertEqual("Vortex-Epic-18:30", objective["notify_role_name"])
        self.assertTrue(objective[objectives._NOTIFY_ROLE_NAME_CLEANUP_FIELD])

    async def test_new_role_uses_transient_name_only_until_id_is_authoritative(self) -> None:
        objective = {
            "id": "objective-owned",
            "type": "Vortex",
            "rarity": "Epic",
            "pop_time_utc": "18:30",
        }
        pending_name = objectives._build_pending_notify_role_name(30, objective)
        role = self._role(pending_name)
        guild = SimpleNamespace(
            id=30,
            roles=[],
            create_role=AsyncMock(return_value=role),
            get_role=lambda role_id: role if role_id == 20 else None,
        )

        with (
            patch.object(objectives.role_security, "self_assignment_error", return_value=None),
            patch.object(objectives, "_notify_role_ownership_error", return_value=None),
        ):
            resolution = await objectives._ensure_notify_role(guild, objective)
            objective.update(
                {
                    "notify_role_id": resolution.role_id,
                    "notify_role_name": resolution.role_name,
                    "notify_role_created_by_bot": resolution.created_by_bot,
                }
            )
            cleaned = await objectives._clean_persisted_notify_role_name(guild, objective)

        self.assertTrue(cleaned)
        guild.create_role.assert_awaited_once_with(
            name=pending_name,
            mentionable=False,
            reason="Objective notification role",
        )
        self.assertIn(
            message_checkpoints.hidden_checkpoint(
                objectives._notify_role_checkpoint_marker(30, objective),
                bit_count=40,
            ),
            pending_name,
        )
        role.edit.assert_awaited_once_with(
            name="Vortex-Epic-18:30",
            reason="Finalize objective notification role name",
        )
        self.assertEqual("Vortex-Epic-18:30", objective["notify_role_name"])


class ObjectiveConfigurationTeardownTests(unittest.IsolatedAsyncioTestCase):
    async def test_teardown_cleans_legacy_objective_checkpoint_before_state_removal(
        self,
    ) -> None:
        marker = objectives._creation_marker(30, "objective-one")
        embed = discord.Embed(title="Objective")
        embed.set_footer(text=marker)
        message = SimpleNamespace(
            author=SimpleNamespace(id=100),
            content=message_checkpoints.content_with_checkpoint("Visible objective", marker),
            embeds=[embed],
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        entry = {
            "panel_channel_id": None,
            "panel_message_id": None,
            "objectives": [
                {
                    "id": "objective-one",
                    "channel_id": 40,
                    "message_id": 50,
                }
            ],
        }
        guild = SimpleNamespace(id=30, me=SimpleNamespace(id=100))

        with (
            patch.object(objectives, "_load_guild_entry", return_value=entry),
            patch.object(objectives, "_save_guild_entry"),
            patch.object(
                objectives,
                "_cleanup_objective_notification_assets",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                objectives,
                "_resolve_text_channel",
                new=AsyncMock(return_value=channel),
            ),
            patch.object(
                objectives,
                "clear_guild_objective_configuration",
                return_value=True,
            ) as clear_configuration,
        ):
            cleaned = await objectives.deactivate_guild_objective_configuration(guild)

        self.assertTrue(cleaned)
        first_edit = message.edit.await_args_list[0].kwargs
        self.assertEqual("Visible objective", first_edit["content"])
        self.assertIsNone(first_edit["embeds"][0].footer.text)
        self.assertEqual({"view": None}, message.edit.await_args_list[1].kwargs)
        clear_configuration.assert_called_once_with(30)


class ObjectiveMentionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_allows_only_subscriber_mentions(self) -> None:
        class FakeTextChannel:
            def __init__(self) -> None:
                self.send = AsyncMock(return_value=SimpleNamespace(id=555))

        channel = FakeTextChannel()
        subscribers = [
            SimpleNamespace(id=111, mention="<@111>", bot=False),
            SimpleNamespace(id=222, mention="<@222>", bot=False),
        ]
        role = SimpleNamespace(id=20, mentionable=False, members=subscribers)
        guild = SimpleNamespace(
            get_channel=lambda channel_id: channel,
            get_role=lambda role_id: role,
        )
        objective = {
            "type": "Vortex",
            "rarity": "Epic",
            "map": "Map <@999>",
            "channel_id": 10,
            "notify_role_id": 20,
            "pop_at_ts": 1_800_000_000,
            "pop_time_utc": "18:30",
        }

        with (
            patch.object(objectives.discord, "TextChannel", FakeTextChannel),
            patch.object(
                objectives,
                "_notify_role_ownership_error",
                return_value=None,
            ),
        ):
            message_id = await objectives._send_objective_notification(
                guild,
                objective,
                fallback_channel_id=None,
                notify_before_minutes=15,
            )

        self.assertEqual(555, message_id)
        channel.send.assert_awaited_once()
        content = channel.send.await_args.args[0]
        self.assertIn("<@111>", content)
        self.assertIn("<@222>", content)
        self.assertIn("<@999>", content)
        allowed_mentions = channel.send.await_args.kwargs["allowed_mentions"]
        self.assertEqual(
            [111, 222],
            [user.id for user in allowed_mentions.users],
        )
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.replied_user)


class ObjectiveSchedulerRobustnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_timestamps_are_treated_as_unset(self) -> None:
        guild = SimpleNamespace(get_role=lambda role_id: None)
        entry = {
            "objectives": [
                {
                    "id": 123,
                    "pop_at_ts": "not-a-number",
                    "remove_at_ts": {},
                    "notified_ts": [],
                    "notify_at_ts": "also-invalid",
                    "notify_before_minutes": 5,
                }
            ]
        }

        result = await objectives._process_guild(guild, entry, now_ts=100)

        self.assertEqual(({}, {}, {}, set(), set(), False), result)

    async def test_one_guild_failure_does_not_starve_the_next_guild(self) -> None:
        first = SimpleNamespace(id=1)
        second = SimpleNamespace(id=2)
        bot = SimpleNamespace(get_guild=lambda guild_id: first if guild_id == 1 else second)
        process = AsyncMock(side_effect=[ValueError("malformed"), None])

        with (
            patch.object(
                objectives,
                "_load_config",
                return_value={"1": {}, "2": {}},
            ),
            patch.object(
                objectives,
                "_process_configured_guild",
                new=process,
            ),
            patch.object(objectives.logging, "exception") as log_exception,
        ):
            await objectives._process_all_guilds(bot, now_ts=100)

        self.assertEqual(2, process.await_count)
        log_exception.assert_called_once()


class ObjectiveDurableActionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _history(messages):
        for message in messages:
            yield message

    async def test_persisted_message_id_is_authoritative_only_for_bot_owned_message(self) -> None:
        owned = SimpleNamespace(
            id=555,
            author=SimpleNamespace(id=42),
            content="No checkpoint",
            embeds=[],
            nonce=None,
        )
        owned_channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=owned),
            history=MagicMock(side_effect=AssertionError("history must not be scanned")),
        )

        found = await objectives._find_marked_message(
            owned_channel,
            "missing marker",
            message_id=555,
            bot_user_id=42,
        )

        self.assertIs(owned, found)
        owned_channel.history.assert_not_called()

        foreign = SimpleNamespace(
            id=555,
            author=SimpleNamespace(id=99),
            content="No checkpoint",
            embeds=[],
            nonce=None,
        )
        foreign_channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=foreign),
            history=lambda **_kwargs: self._history([]),
        )

        not_found = await objectives._find_marked_message(
            foreign_channel,
            "missing marker",
            message_id=555,
            bot_user_id=42,
        )

        self.assertIsNone(not_found)

    async def test_completed_records_are_included_in_one_time_restart_cleanup(self) -> None:
        def record(kind: str, external_id: str):
            return SimpleNamespace(
                kind=kind,
                guild_id=30,
                external_id=external_id,
                status="completed",
                payload={},
            )

        records = {
            objectives._PANEL_PUBLICATION_RUNTIME_KIND: record(
                objectives._PANEL_PUBLICATION_RUNTIME_KIND,
                "panel",
            ),
            objectives._CREATION_RUNTIME_KIND: record(
                objectives._CREATION_RUNTIME_KIND,
                "id:objective-a",
            ),
            objectives._NOTIFICATION_RUNTIME_KIND: record(
                objectives._NOTIFICATION_RUNTIME_KIND,
                "id:objective-a",
            ),
        }

        def list_records(kind, *, guild_id, statuses):
            self.assertEqual(30, guild_id)
            return [records[kind]] if statuses == ("completed",) else []

        guild = SimpleNamespace(id=30)
        with (
            patch.object(objectives.runtime_state, "list_records", side_effect=list_records),
            patch.object(
                objectives,
                "_cleanup_completed_panel_record",
                new=AsyncMock(return_value=True),
            ) as clean_panel,
            patch.object(
                objectives,
                "_cleanup_completed_creation_record",
                new=AsyncMock(return_value=True),
            ) as clean_creation,
            patch.object(
                objectives,
                "_cleanup_completed_notification_record",
                new=AsyncMock(return_value=True),
            ) as clean_notification,
            patch.object(
                objectives,
                "_reconcile_active_notify_role_names",
                new=AsyncMock(),
            ),
        ):
            await objectives._reconcile_objective_actions_for_guild(guild)

        clean_panel.assert_awaited_once_with(
            guild, records[objectives._PANEL_PUBLICATION_RUNTIME_KIND]
        )
        clean_creation.assert_awaited_once_with(guild, records[objectives._CREATION_RUNTIME_KIND])
        clean_notification.assert_awaited_once_with(
            guild,
            records[objectives._NOTIFICATION_RUNTIME_KIND],
        )

    async def test_active_messages_are_cleaned_without_historical_action_rows(self) -> None:
        objective = {
            "id": "objective-a",
            "channel_id": 10,
            "message_id": 501,
            "notify_message_id": 502,
        }
        objective_key = objectives._objective_key(objective)
        panel_marker = f"{objectives._panel_marker(30)}:lost-operation"
        objective_marker = objectives._creation_marker(30, objective_key)
        notification_marker = objectives._notification_marker(30, objective_key)

        panel_embed = discord.Embed(title="Active objectives")
        panel_embed.set_footer(text=panel_marker)
        panel_message = SimpleNamespace(
            author=SimpleNamespace(id=42),
            content="",
            embeds=[panel_embed],
            edit=AsyncMock(),
        )
        objective_message = SimpleNamespace(
            author=SimpleNamespace(id=42),
            content=message_checkpoints.content_with_checkpoint(None, objective_marker),
            embeds=[discord.Embed(title="Objective")],
            edit=AsyncMock(),
        )
        notification_message = SimpleNamespace(
            author=SimpleNamespace(id=42),
            content=message_checkpoints.content_with_checkpoint(
                "<@123> Objective soon",
                notification_marker,
            ),
            embeds=[],
            edit=AsyncMock(),
        )
        messages = {500: panel_message, 501: objective_message, 502: notification_message}
        channel = SimpleNamespace(
            fetch_message=AsyncMock(side_effect=lambda message_id: messages[message_id]),
        )
        guild = SimpleNamespace(id=30, me=SimpleNamespace(id=42))

        with (
            patch.object(
                objectives,
                "_active_objectives_entry",
                return_value={
                    "panel_channel_id": 10,
                    "panel_message_id": 500,
                    "objectives": [objective],
                },
            ),
            patch.object(
                objectives,
                "_resolve_text_channel",
                new=AsyncMock(return_value=channel),
            ),
        ):
            cleaned = await objectives._clean_active_objective_message_checkpoints(guild)

        self.assertTrue(cleaned)
        self.assertIsNone(panel_message.edit.await_args.kwargs["embeds"][0].footer.text)
        self.assertIsNone(objective_message.edit.await_args.kwargs["content"])
        self.assertEqual(
            "<@123> Objective soon",
            notification_message.edit.await_args.kwargs["content"],
        )

    async def test_notification_recovery_discovers_marked_message_without_resending(self) -> None:
        objective = {
            "id": "objective-a",
            "pop_at_ts": 2_000_000_000,
            "notify_before_minutes": 15,
        }
        key = objectives._objective_key(objective)
        marker = objectives._notification_marker(30, key)
        real_embed = discord.Embed(title="Keep me")
        legacy_marker_embed = discord.Embed()
        legacy_marker_embed.set_footer(text=marker)
        sent = SimpleNamespace(
            id=555,
            author=SimpleNamespace(id=42),
            content="Visible notification",
            nonce=None,
            embeds=[real_embed, legacy_marker_embed],
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(
            send=AsyncMock(),
            fetch_message=AsyncMock(return_value=sent),
            history=lambda **_kwargs: self._history([sent]),
        )
        guild = SimpleNamespace(id=30, me=SimpleNamespace(id=42))
        record = SimpleNamespace(
            external_id=key,
            payload={
                "channel_id": 10,
                "marker": marker,
                "notified_ts": 1_900_000_000,
            },
        )
        completed_record = SimpleNamespace(
            kind=objectives._NOTIFICATION_RUNTIME_KIND,
            guild_id=30,
            external_id=key,
            status="completed",
            payload={
                "channel_id": 10,
                "message_id": 555,
                "marker": marker,
                "notified_ts": 1_900_000_000,
            },
        )

        with (
            patch.object(
                objectives,
                "_active_objectives_entry",
                return_value={"panel_channel_id": 10, "objectives": [objective]},
            ),
            patch.object(
                objectives,
                "_resolve_text_channel",
                new=AsyncMock(return_value=channel),
            ),
            patch.object(objectives, "_apply_objective_deltas") as apply_deltas,
            patch.object(objectives.runtime_state, "get_record", return_value=completed_record),
            patch.object(objectives.runtime_state, "upsert_record") as upsert_record,
            patch.object(objectives.runtime_state, "set_status") as set_status,
        ):
            await objectives._reconcile_pending_notification(guild, record)

        channel.send.assert_not_awaited()
        self.assertEqual(555, apply_deltas.call_args.args[3][key])
        set_status.assert_called_with(
            objectives._NOTIFICATION_RUNTIME_KIND,
            30,
            key,
            "completed",
        )
        sent.edit.assert_awaited_once()
        retained_embeds = sent.edit.await_args.kwargs["embeds"]
        self.assertEqual(["Keep me"], [embed.title for embed in retained_embeds])
        self.assertIsNone(retained_embeds[0].footer.text)
        cleanup_payload = upsert_record.call_args_list[-1].args[3]
        self.assertTrue(cleanup_payload[objectives._MESSAGE_CHECKPOINT_CLEANUP_FIELD])
        self.assertEqual("completed", upsert_record.call_args_list[-1].kwargs["status"])

    async def test_notification_intent_is_persisted_before_discord_send(self) -> None:
        events = []
        sent_options = {}
        sent = SimpleNamespace(
            id=555,
            author=SimpleNamespace(id=42),
            content="",
            embeds=[],
            nonce=None,
            edit=AsyncMock(side_effect=lambda **_kwargs: events.append("clean")),
        )

        async def send(content, **kwargs):
            events.append("send")
            sent_options.update(kwargs)
            sent.content = content
            sent.nonce = kwargs.get("nonce")
            return sent

        channel = SimpleNamespace(send=send)
        role = SimpleNamespace(
            id=20,
            mentionable=False,
            members=[SimpleNamespace(id=111, mention="<@111>", bot=False)],
        )
        guild = SimpleNamespace(
            id=30,
            me=SimpleNamespace(id=42),
            get_channel=lambda _channel_id: channel,
            get_role=lambda _role_id: role,
        )
        objective = {
            "id": "objective-a",
            "type": "Vortex",
            "rarity": "Epic",
            "map": "Arthur's Rest",
            "channel_id": 10,
            "notify_role_id": 20,
            "pop_at_ts": 2_000_000_000,
            "notify_before_minutes": 15,
        }

        def persist(*_args, **kwargs):
            events.append(kwargs["status"])
            return SimpleNamespace(payload={})

        with (
            patch.object(objectives.discord, "TextChannel", type(channel)),
            patch.object(objectives, "_notify_role_ownership_error", return_value=None),
            patch.object(objectives, "_find_marked_message", new=AsyncMock(return_value=None)),
            patch.object(objectives.runtime_state, "get_record", return_value=None),
            patch.object(objectives.runtime_state, "upsert_record", side_effect=persist),
            patch.object(objectives.runtime_state, "set_status"),
            patch.object(
                objectives,
                "_load_guild_entry",
                return_value={"objectives": [objective]},
            ),
            patch.object(objectives, "_apply_objective_deltas"),
        ):
            message_id = await objectives._send_objective_notification(
                guild,
                objective,
                fallback_channel_id=None,
                notify_before_minutes=15,
                notified_ts=1_900_000_000,
            )

        self.assertEqual(555, message_id)
        self.assertEqual(["pending", "send", "sent", "clean", "completed"], events)
        marker = objectives._notification_marker(30, objectives._objective_key(objective))
        self.assertNotIn(marker, sent.content)
        self.assertIn(message_checkpoints.hidden_checkpoint(marker), sent.content)
        self.assertEqual(message_checkpoints.stable_nonce(marker), sent.nonce)
        self.assertNotIn("embed", sent_options)
        self.assertNotIn("embeds", sent_options)
        sent.edit.assert_awaited_once()
        self.assertNotIn(
            message_checkpoints.hidden_checkpoint(marker),
            sent.edit.await_args.kwargs["content"],
        )

    async def test_panel_publication_recovers_marked_message_after_send_crash(self) -> None:
        marker = objectives._panel_marker(30)
        legacy_panel_embed = discord.Embed(title="Active objectives:")
        legacy_panel_embed.set_footer(text=marker)
        sent = SimpleNamespace(
            id=555,
            author=SimpleNamespace(id=42),
            content="",
            nonce=None,
            embeds=[legacy_panel_embed],
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(
            send=AsyncMock(),
            history=lambda **_kwargs: self._history([sent]),
        )
        guild = SimpleNamespace(id=30, me=SimpleNamespace(id=42))
        record = SimpleNamespace(
            external_id="panel",
            payload={"target_channel_id": 10, "marker": marker},
        )
        ready_record = SimpleNamespace(
            external_id="panel",
            payload={"target_channel_id": 10, "marker": marker, "message_id": 555},
        )

        with (
            patch.object(objectives, "_load_config", return_value={}),
            patch.object(objectives.guild_settings, "get_target_guild", return_value="guild"),
            patch.object(
                objectives,
                "_resolve_text_channel",
                new=AsyncMock(return_value=channel),
            ),
            patch.object(
                objectives,
                "_persist_panel_publication",
                return_value=ready_record,
            ),
            patch.object(
                objectives, "set_objectives_panel_message", return_value=True
            ) as save_panel,
            patch.object(objectives.runtime_state, "upsert_record") as upsert_record,
            patch.object(objectives.runtime_state, "set_status") as set_status,
        ):
            message_id = await objectives._complete_panel_publication(guild, record)

        self.assertEqual(555, message_id)
        channel.send.assert_not_awaited()
        save_panel.assert_called_once_with(30, 10, 555)
        set_status.assert_called_with(
            objectives._PANEL_PUBLICATION_RUNTIME_KIND,
            30,
            "panel",
            "completed",
        )
        sent.edit.assert_awaited_once()
        cleaned_embed = sent.edit.await_args.kwargs["embeds"][0]
        self.assertEqual("Active objectives:", cleaned_embed.title)
        self.assertIsNone(cleaned_embed.footer.text)
        cleanup_payload = upsert_record.call_args.args[3]
        self.assertTrue(cleanup_payload[objectives._MESSAGE_CHECKPOINT_CLEANUP_FIELD])

    async def test_new_panel_checkpoint_is_hidden_until_config_commit(self) -> None:
        events = []
        sent_options = {}
        marker = objectives._new_panel_marker(30)
        sent = SimpleNamespace(
            id=555,
            author=SimpleNamespace(id=42),
            content="",
            embeds=[],
            nonce=None,
            edit=AsyncMock(side_effect=lambda **_kwargs: events.append("clean")),
        )

        async def empty_history(**_kwargs):
            if False:
                yield None

        async def send(**kwargs):
            events.append("send")
            sent_options.update(kwargs)
            sent.content = kwargs.get("content") or ""
            sent.embeds = [kwargs["embed"]]
            sent.nonce = kwargs.get("nonce")
            return sent

        channel = SimpleNamespace(send=send, history=empty_history)
        guild = SimpleNamespace(id=30, me=SimpleNamespace(id=42))
        record = SimpleNamespace(
            external_id="panel",
            payload={"target_channel_id": 10, "marker": marker},
        )
        ready_record = SimpleNamespace(
            external_id="panel",
            payload={"target_channel_id": 10, "marker": marker, "message_id": 555},
        )

        def persist(*_args, **kwargs):
            events.append(kwargs["status"])
            return ready_record

        def save_panel(*_args):
            events.append("config")
            return True

        with (
            patch.object(objectives, "_load_guild_entry", return_value={}),
            patch.object(objectives.guild_settings, "get_target_guild", return_value="guild"),
            patch.object(
                objectives,
                "_resolve_text_channel",
                new=AsyncMock(return_value=channel),
            ),
            patch.object(objectives, "_persist_panel_publication", side_effect=persist),
            patch.object(objectives, "set_objectives_panel_message", side_effect=save_panel),
            patch.object(objectives.runtime_state, "upsert_record") as upsert_record,
            patch.object(objectives.runtime_state, "set_status"),
        ):
            message_id = await objectives._complete_panel_publication(guild, record)

        self.assertEqual(555, message_id)
        self.assertEqual(["send", "message_ready", "config", "clean"], events)
        self.assertEqual(message_checkpoints.hidden_checkpoint(marker), sent_options["content"])
        self.assertEqual(message_checkpoints.stable_nonce(marker), sent_options["nonce"])
        self.assertIsNone(sent_options["embed"].footer.text)
        sent.edit.assert_awaited_once()
        self.assertIsNone(sent.edit.await_args.kwargs["content"])
        cleanup_mentions = sent.edit.await_args.kwargs["allowed_mentions"]
        self.assertFalse(cleanup_mentions.everyone)
        self.assertFalse(cleanup_mentions.users)
        self.assertFalse(cleanup_mentions.roles)
        cleanup_payload = upsert_record.call_args.args[3]
        self.assertTrue(cleanup_payload[objectives._MESSAGE_CHECKPOINT_CLEANUP_FIELD])

    async def test_panel_command_persists_intent_before_publication(self) -> None:
        events = []
        record = SimpleNamespace(external_id="panel", payload={"target_channel_id": 10})
        guild = SimpleNamespace(id=30)
        channel = SimpleNamespace(id=10)
        actor = SimpleNamespace(id=20)

        def persist(*_args, **_kwargs):
            events.append("pending")
            return record

        async def complete(*_args, **_kwargs):
            events.append("publish")
            return 555

        with (
            patch.object(objectives, "_objective_setup_is_authorized", return_value=True),
            patch.object(objectives, "_load_config", return_value={}),
            patch.object(objectives, "get_objectives_panel_message", return_value=(None, None)),
            patch.object(objectives, "_persist_panel_publication", side_effect=persist),
            patch.object(
                objectives,
                "_complete_panel_publication",
                new=AsyncMock(side_effect=complete),
            ),
        ):
            ok, _message = await objectives.post_or_update_objectives_panel(
                guild,
                channel,
                actor,
            )

        self.assertTrue(ok)
        self.assertEqual(["pending", "publish"], events)

    async def test_pending_objective_message_is_discovered_and_committed_once(self) -> None:
        objective = {
            "id": "objective-a",
            "type": "Vortex",
            "rarity": "Epic",
            "map": "Arthur's Rest",
            "pop_at_ts": 2_000_000_000,
            "notify_before_minutes": 15,
        }
        key = objectives._objective_key(objective)
        marker = objectives._creation_marker(30, key)
        legacy_objective_embed = objectives._build_objective_embed(objective)
        legacy_objective_embed.set_footer(text=marker)
        posted = SimpleNamespace(
            id=555,
            author=SimpleNamespace(id=42),
            content="",
            nonce=None,
            embeds=[legacy_objective_embed],
            edit=AsyncMock(),
        )
        panel_message = SimpleNamespace(edit=AsyncMock())
        panel_channel = SimpleNamespace(
            id=10,
            send=AsyncMock(),
            fetch_message=AsyncMock(return_value=panel_message),
            history=lambda **_kwargs: self._history([posted]),
        )
        guild = SimpleNamespace(
            id=30,
            me=SimpleNamespace(id=42),
            roles=[],
            get_role=lambda _role_id: None,
        )
        record = SimpleNamespace(
            external_id=key,
            status="role_ready",
            payload={
                "objective_key": key,
                "objective": objective,
                "panel_channel_id": 10,
                "panel_message_id": 99,
                "marker": marker,
                "role_creation_attempted": True,
            },
        )

        def persist(_guild_id, _key, saved_objective, *_args, **kwargs):
            payload = dict(record.payload)
            payload["objective"] = dict(saved_objective)
            payload.update(kwargs.get("extra") or {})
            return SimpleNamespace(external_id=key, payload=payload, status=kwargs["status"])

        with (
            patch.object(
                objectives,
                "_active_objectives_entry",
                return_value={"objectives": []},
            ),
            patch.object(objectives, "_objectives_panel_is_current", return_value=True),
            patch.object(
                objectives,
                "_resolve_text_channel",
                new=AsyncMock(return_value=panel_channel),
            ),
            patch.object(objectives, "_persist_creation_state", side_effect=persist),
            patch.object(objectives, "add_objective", return_value=True) as add_objective,
            patch.object(objectives.runtime_state, "upsert_record") as upsert_record,
            patch.object(objectives.runtime_state, "set_status"),
        ):
            result = await objectives._complete_pending_objective_creation(guild, record)

        self.assertTrue(result.success)
        panel_channel.send.assert_not_awaited()
        saved_objective = add_objective.call_args.args[1]
        self.assertEqual(555, saved_objective["message_id"])
        posted.edit.assert_awaited_once()
        cleaned_embed = posted.edit.await_args.kwargs["embeds"][0]
        self.assertEqual(legacy_objective_embed.title, cleaned_embed.title)
        self.assertEqual(len(legacy_objective_embed.fields), len(cleaned_embed.fields))
        self.assertIsNone(cleaned_embed.footer.text)
        cleanup_payload = upsert_record.call_args.args[3]
        self.assertTrue(cleanup_payload[objectives._MESSAGE_CHECKPOINT_CLEANUP_FIELD])

    async def test_new_objective_checkpoint_is_hidden_until_objective_commit(self) -> None:
        events = []
        sent_options = {}
        objective = {
            "id": "objective-a",
            "type": "Vortex",
            "rarity": "Epic",
            "map": "Arthur's Rest",
            "pop_at_ts": 2_000_000_000,
            "notify_before_minutes": 15,
        }
        key = objectives._objective_key(objective)
        marker = objectives._creation_marker(30, key)
        sent = SimpleNamespace(
            id=555,
            author=SimpleNamespace(id=42),
            content="",
            embeds=[],
            nonce=None,
            edit=AsyncMock(side_effect=lambda **_kwargs: events.append("clean")),
        )

        async def empty_history(**_kwargs):
            if False:
                yield None

        async def send(**kwargs):
            events.append("send")
            sent_options.update(kwargs)
            sent.content = kwargs.get("content") or ""
            sent.embeds = [kwargs["embed"]]
            sent.nonce = kwargs.get("nonce")
            return sent

        panel_message = SimpleNamespace(edit=AsyncMock())
        panel_channel = SimpleNamespace(
            id=10,
            send=send,
            fetch_message=AsyncMock(return_value=panel_message),
            history=empty_history,
        )
        guild = SimpleNamespace(
            id=30,
            me=SimpleNamespace(id=42),
            roles=[],
            get_role=lambda _role_id: None,
        )
        record = SimpleNamespace(
            external_id=key,
            status="role_ready",
            payload={
                "objective_key": key,
                "objective": objective,
                "panel_channel_id": 10,
                "panel_message_id": 99,
                "marker": marker,
                "role_creation_attempted": True,
            },
        )

        def persist(_guild_id, _key, saved_objective, *_args, **kwargs):
            events.append(kwargs["status"])
            payload = dict(record.payload)
            payload["objective"] = dict(saved_objective)
            payload.update(kwargs.get("extra") or {})
            return SimpleNamespace(
                external_id=key,
                payload=payload,
                status=kwargs["status"],
            )

        def add(*_args, **_kwargs):
            events.append("config")
            return True

        with (
            patch.object(objectives, "_active_objectives_entry", return_value={"objectives": []}),
            patch.object(objectives, "_objectives_panel_is_current", return_value=True),
            patch.object(
                objectives,
                "_resolve_text_channel",
                new=AsyncMock(return_value=panel_channel),
            ),
            patch.object(objectives, "_persist_creation_state", side_effect=persist),
            patch.object(objectives, "add_objective", side_effect=add),
            patch.object(objectives.runtime_state, "upsert_record") as upsert_record,
            patch.object(objectives.runtime_state, "set_status"),
        ):
            result = await objectives._complete_pending_objective_creation(guild, record)

        self.assertTrue(result.success)
        self.assertEqual(["role_ready", "send", "message_ready", "config", "clean"], events)
        self.assertEqual(message_checkpoints.hidden_checkpoint(marker), sent_options["content"])
        self.assertEqual(message_checkpoints.stable_nonce(marker), sent_options["nonce"])
        self.assertIsNone(sent_options["embed"].footer.text)
        self.assertNotIn("embeds", sent_options)
        sent.edit.assert_awaited_once()
        self.assertIsNone(sent.edit.await_args.kwargs["content"])
        cleanup_payload = upsert_record.call_args.args[3]
        self.assertTrue(cleanup_payload[objectives._MESSAGE_CHECKPOINT_CLEANUP_FIELD])

    async def test_pending_role_is_discovered_by_zero_width_checkpoint(self) -> None:
        objective = {"id": "objective-a", "type": "Core", "pop_time_utc": "18:30"}
        role = SimpleNamespace(
            id=20,
            name=objectives._build_pending_notify_role_name(30, objective),
        )
        guild = SimpleNamespace(
            id=30,
            roles=[role],
            get_role=lambda _role_id: None,
            create_role=AsyncMock(),
        )

        with patch.object(
            objectives.role_security,
            "self_assignment_error",
            return_value=None,
        ):
            resolution = await objectives._ensure_notify_role(guild, objective)

        self.assertEqual(20, resolution.role_id)
        self.assertEqual("Core-?-18:30", resolution.role_name)
        guild.create_role.assert_not_awaited()

    async def test_clean_name_is_not_adopted_without_authoritative_role_id(self) -> None:
        objective = {"id": "objective-a", "type": "Core", "pop_time_utc": "18:30"}
        unrelated = SimpleNamespace(
            id=20,
            name=objectives._build_notify_role_name(objective),
        )
        created = SimpleNamespace(
            id=21,
            name=objectives._build_pending_notify_role_name(30, objective),
        )
        guild = SimpleNamespace(
            id=30,
            roles=[unrelated],
            get_role=lambda _role_id: None,
            create_role=AsyncMock(return_value=created),
        )

        with patch.object(
            objectives.role_security,
            "self_assignment_error",
            return_value=None,
        ):
            resolution = await objectives._ensure_notify_role(guild, objective)

        self.assertEqual(21, resolution.role_id)
        guild.create_role.assert_awaited_once_with(
            name=objectives._build_pending_notify_role_name(30, objective),
            mentionable=False,
            reason="Objective notification role",
        )
        self.assertNotRegex(
            guild.create_role.await_args.kwargs["name"],
            r"-[0-9a-f]{10}$",
        )


class ObjectiveSchedulerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_is_cancelled_and_reset_during_shutdown(self) -> None:
        await objectives.stop_objectives_scheduler()
        started = asyncio.Event()

        async def worker(_bot):
            started.set()
            await asyncio.Event().wait()

        with patch.object(objectives, "_objectives_scheduler_loop", side_effect=worker):
            objectives.start_objectives_scheduler(SimpleNamespace())
            await started.wait()
            task = objectives._scheduler_task
            await objectives.stop_objectives_scheduler()

        self.assertIsNotNone(task)
        self.assertTrue(task.done())
        self.assertIsNone(objectives._scheduler_task)


if __name__ == "__main__":
    unittest.main()
