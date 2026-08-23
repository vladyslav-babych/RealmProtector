import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.realm_protector.bot import reaction_roles
from src.realm_protector.infrastructure.runtime_state import RuntimeRecord


class ReactionRoleRuntimeSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_add_is_inert_after_main_configuration_removal(self) -> None:
        guild = SimpleNamespace(id=42)
        bot = SimpleNamespace(
            user=SimpleNamespace(id=100),
            get_guild=lambda guild_id: guild,
        )
        payload = SimpleNamespace(
            guild_id=42,
            user_id=200,
            message_id=300,
            emoji="🔥",
        )

        with (
            patch.object(
                reaction_roles.guild_settings,
                "get_target_guild",
                return_value=None,
            ),
            patch.object(reaction_roles, "_get_panel_by_message_id") as get_panel,
        ):
            await reaction_roles.handle_raw_reaction_add(bot, payload)

        get_panel.assert_not_called()

    async def test_panel_is_only_made_functional_after_message_id_is_durable(self) -> None:
        events = []
        message = SimpleNamespace(
            id=300,
            channel=SimpleNamespace(id=200),
            edit=AsyncMock(side_effect=lambda **_kwargs: events.append("edit")),
            add_reaction=AsyncMock(side_effect=lambda _emoji: events.append("reaction")),
        )
        destination = SimpleNamespace(
            id=200,
            send=AsyncMock(
                side_effect=lambda **kwargs: (
                    events.append(("send", kwargs)),
                    message,
                )[1]
            ),
        )
        guild = SimpleNamespace(
            id=42,
            get_role=lambda role_id: SimpleNamespace(id=role_id, mention="@role"),
        )
        panel = {
            "id": "panel-one",
            "panel_name": "Roles",
            "panel_message": "Choose",
            "destination_channel_id": 200,
            "reactions": [{"emoji": "🔥", "role_id": 99}],
        }

        def record(_guild_id, _operation_id, current_panel, *, operation, status):
            events.append(("record", status, current_panel.get("panel_message_id"), operation))

        with (
            patch.object(
                reaction_roles,
                "uuid4",
                return_value=SimpleNamespace(hex="operation-one"),
            ),
            patch.object(reaction_roles, "_record_publish", side_effect=record),
        ):
            published, operation_id = await reaction_roles._post_pending_panel(
                guild,
                destination,
                panel,
                operation="create",
            )

        self.assertIs(message, published)
        self.assertEqual("operation-one", operation_id)
        self.assertEqual(
            [
                ("record", "prepared", None, "create"),
                ("send", destination.send.await_args.kwargs),
                ("record", "placeholder_created", 300, "create"),
                "edit",
                "reaction",
                ("record", "ready_to_commit", 300, "create"),
            ],
            events,
        )
        placeholder = destination.send.await_args.kwargs
        marker = reaction_roles._publish_marker("operation-one")
        self.assertEqual(
            "Preparing reaction-role panel.",
            reaction_roles.message_checkpoints.strip_checkpoint(
                placeholder["content"],
                marker,
            ),
        )
        self.assertNotIn("operation-one", placeholder["content"])
        self.assertEqual(
            reaction_roles.message_checkpoints.stable_nonce(marker),
            placeholder["nonce"],
        )
        self.assertNotIn("embed", placeholder)
        final_options = message.edit.await_args.kwargs
        self.assertIsNone(final_options["content"])
        self.assertIsNone(final_options["embed"].footer.text)

    async def test_reaction_failure_compensates_discord_message(self) -> None:
        message = SimpleNamespace(
            id=300,
            channel=SimpleNamespace(id=200),
            edit=AsyncMock(),
            delete=AsyncMock(),
            clear_reactions=AsyncMock(),
        )
        destination = SimpleNamespace(id=200, send=AsyncMock(return_value=message))
        guild = SimpleNamespace(
            id=42,
            get_role=lambda role_id: SimpleNamespace(id=role_id, mention="@role"),
        )
        panel = {
            "id": "panel-one",
            "panel_name": "Roles",
            "panel_message": "Choose",
            "destination_channel_id": 200,
            "reactions": [{"emoji": "🔥", "role_id": 99}],
        }

        with (
            patch.object(
                reaction_roles,
                "uuid4",
                return_value=SimpleNamespace(hex="operation-one"),
            ),
            patch.object(reaction_roles, "_record_publish"),
            patch.object(reaction_roles, "_add_panel_reaction", return_value=False),
            patch.object(reaction_roles.runtime_state, "delete_record") as delete_record,
        ):
            with self.assertRaises(reaction_roles._PanelPublishError):
                await reaction_roles._post_pending_panel(
                    guild,
                    destination,
                    panel,
                    operation="create",
                )

        message.delete.assert_awaited_once_with()
        delete_record.assert_called_once_with(
            reaction_roles._PUBLISH_RUNTIME_KIND,
            42,
            "operation-one",
        )

    async def test_storage_failure_happens_before_any_discord_post(self) -> None:
        destination = SimpleNamespace(id=200, send=AsyncMock())
        guild = SimpleNamespace(id=42)
        panel = {
            "id": "panel-one",
            "destination_channel_id": 200,
            "reactions": [{"emoji": "🔥", "role_id": 99}],
        }

        with patch.object(
            reaction_roles,
            "_record_publish",
            side_effect=OSError("storage unavailable"),
        ):
            with self.assertRaisesRegex(
                reaction_roles._PanelPublishError,
                "no Discord panel was posted",
            ):
                await reaction_roles._post_pending_panel(
                    guild,
                    destination,
                    panel,
                    operation="create",
                )

        destination.send.assert_not_awaited()

    async def test_reconciliation_keeps_committed_panel(self) -> None:
        record = RuntimeRecord(
            kind=reaction_roles._PUBLISH_RUNTIME_KIND,
            guild_id=42,
            external_id="operation-one",
            payload={
                "panel_id": "panel-one",
                "panel": {
                    "id": "panel-one",
                    "panel_channel_id": 200,
                    "panel_message_id": 300,
                },
            },
            status="ready_to_commit",
            updated_at="now",
        )
        bot = SimpleNamespace(get_guild=lambda _guild_id: None)

        with (
            patch.object(reaction_roles.runtime_state, "list_records", return_value=[record]),
            patch.object(reaction_roles, "_publication_was_committed", return_value=True),
            patch.object(reaction_roles.runtime_state, "delete_record") as delete_record,
            patch.object(reaction_roles, "_resolve_pending_publish_message") as resolve,
        ):
            await reaction_roles.reconcile_reaction_role_panels(bot)

        delete_record.assert_called_once_with(
            reaction_roles._PUBLISH_RUNTIME_KIND,
            42,
            "operation-one",
        )
        resolve.assert_not_called()

    async def test_reconciliation_deletes_uncommitted_panel_message(self) -> None:
        record = RuntimeRecord(
            kind=reaction_roles._PUBLISH_RUNTIME_KIND,
            guild_id=42,
            external_id="operation-one",
            payload={
                "panel_id": "panel-one",
                "panel": {
                    "id": "panel-one",
                    "panel_channel_id": 200,
                    "panel_message_id": 300,
                },
            },
            status="ready_to_commit",
            updated_at="now",
        )
        message = SimpleNamespace(delete=AsyncMock())
        guild = SimpleNamespace(id=42)
        bot = SimpleNamespace(get_guild=lambda _guild_id: guild)

        with (
            patch.object(reaction_roles.runtime_state, "list_records", return_value=[record]),
            patch.object(reaction_roles, "_publication_was_committed", return_value=False),
            patch.object(
                reaction_roles,
                "_resolve_pending_publish_message",
                new=AsyncMock(return_value=message),
            ),
            patch.object(reaction_roles.runtime_state, "delete_record") as delete_record,
        ):
            await reaction_roles.reconcile_reaction_role_panels(bot)

        message.delete.assert_awaited_once_with()
        delete_record.assert_called_once_with(
            reaction_roles._PUBLISH_RUNTIME_KIND,
            42,
            "operation-one",
        )

    async def test_reconciliation_finds_hidden_placeholder_when_crash_preceded_id_write(
        self,
    ) -> None:
        record = RuntimeRecord(
            kind=reaction_roles._PUBLISH_RUNTIME_KIND,
            guild_id=42,
            external_id="operation-one",
            payload={
                "panel_id": "panel-one",
                "panel": {
                    "id": "panel-one",
                    "destination_channel_id": 200,
                },
            },
            status="prepared",
            updated_at="now",
        )
        marker = reaction_roles._publish_marker("operation-one")
        placeholder = SimpleNamespace(
            content=reaction_roles.message_checkpoints.content_with_checkpoint(
                "Preparing reaction-role panel.",
                marker,
            ),
            nonce=None,
            author=SimpleNamespace(id=100),
            embeds=[],
        )

        async def history(*, limit):
            self.assertIsNone(limit)
            yield SimpleNamespace(
                content="unrelated",
                nonce=None,
                author=SimpleNamespace(id=100),
                embeds=[],
            )
            yield placeholder

        channel = SimpleNamespace(history=history)
        guild = SimpleNamespace(
            get_channel=lambda channel_id: channel if channel_id == 200 else None,
            me=SimpleNamespace(id=100),
        )

        with patch.object(
            reaction_roles.discord.abc,
            "Messageable",
            type(channel),
        ):
            resolved = await reaction_roles._resolve_pending_publish_message(
                guild,
                record,
            )

        self.assertIs(placeholder, resolved)

    def test_legacy_publish_footer_is_still_recognized(self) -> None:
        marker = reaction_roles._publish_marker("operation-one")
        embed = reaction_roles.discord.Embed(title="Legacy panel")
        embed.set_footer(text=marker)

        self.assertTrue(
            reaction_roles._message_has_publish_marker(
                SimpleNamespace(content=None, nonce=None, embeds=[embed]),
                "operation-one",
            )
        )

    async def test_committed_legacy_panel_footer_is_removed_without_losing_embed(self) -> None:
        embed = reaction_roles.discord.Embed(title="Roles", description="Choose a role")
        embed.set_footer(text=reaction_roles._publish_marker("operation-one"))
        message = SimpleNamespace(embeds=[embed], edit=AsyncMock())

        cleaned = await reaction_roles._clean_legacy_publish_footers(message)

        self.assertTrue(cleaned)
        cleaned_embed = message.edit.await_args.kwargs["embeds"][0]
        self.assertEqual("Roles", cleaned_embed.title)
        self.assertEqual("Choose a role", cleaned_embed.description)
        self.assertIsNone(cleaned_embed.footer.text)

    async def test_disabled_retained_panel_is_included_in_checkpoint_sweep(self) -> None:
        embed = reaction_roles.discord.Embed(title="Roles")
        embed.set_footer(text=reaction_roles._publish_marker("operation-one"))
        message = SimpleNamespace(
            author=SimpleNamespace(id=100),
            content=None,
            embeds=[embed],
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        guild = SimpleNamespace(
            id=42,
            me=SimpleNamespace(id=100),
            get_channel=lambda channel_id: channel if channel_id == 200 else None,
        )
        entry = {
            "disabled": True,
            "panels": {
                "panel-one": {
                    "panel_channel_id": 200,
                    "panel_message_id": 300,
                }
            },
        }

        with (
            patch.object(reaction_roles, "_load_guild_entry", return_value=entry),
            patch.object(reaction_roles.discord.abc, "Messageable", type(channel)),
        ):
            await reaction_roles._clean_committed_panel_checkpoints_for_guild(guild)

        cleaned_embed = message.edit.await_args.kwargs["embeds"][0]
        self.assertEqual("Roles", cleaned_embed.title)
        self.assertIsNone(cleaned_embed.footer.text)

    async def test_reconciliation_checks_committed_panels_without_publish_records(self) -> None:
        guild = SimpleNamespace(id=42)
        bot = SimpleNamespace(guilds=[guild])

        with (
            patch.object(reaction_roles.runtime_state, "list_records", return_value=[]),
            patch.object(
                reaction_roles,
                "_clean_committed_panel_checkpoints_for_guild",
                new=AsyncMock(),
            ) as clean_committed,
            patch.object(
                reaction_roles,
                "_reconcile_reaction_assignments_for_guild",
                new=AsyncMock(),
            ),
        ):
            await reaction_roles.reconcile_reaction_role_panels(bot)

        clean_committed.assert_awaited_once_with(guild)

    async def test_reconciliation_still_finds_legacy_visible_placeholder(self) -> None:
        record = RuntimeRecord(
            kind=reaction_roles._PUBLISH_RUNTIME_KIND,
            guild_id=42,
            external_id="operation-one",
            payload={
                "panel_id": "panel-one",
                "panel": {
                    "id": "panel-one",
                    "destination_channel_id": 200,
                },
            },
            status="prepared",
            updated_at="now",
        )
        placeholder = SimpleNamespace(
            content=(
                f"Preparing reaction-role panel.\n{reaction_roles._publish_marker('operation-one')}"
            ),
            nonce=None,
            author=SimpleNamespace(id=100),
            embeds=[],
        )

        async def history(*, limit):
            self.assertIsNone(limit)
            yield placeholder

        channel = SimpleNamespace(history=history)
        guild = SimpleNamespace(
            get_channel=lambda channel_id: channel if channel_id == 200 else None,
            me=SimpleNamespace(id=100),
        )

        with patch.object(
            reaction_roles.discord.abc,
            "Messageable",
            type(channel),
        ):
            resolved = await reaction_roles._resolve_pending_publish_message(
                guild,
                record,
            )

        self.assertIs(placeholder, resolved)

    async def test_history_recovery_ignores_checkpoint_from_another_author(self) -> None:
        marker = reaction_roles._publish_marker("operation-one")
        impostor = SimpleNamespace(
            content=reaction_roles.message_checkpoints.content_with_checkpoint(
                "Preparing reaction-role panel.",
                marker,
            ),
            nonce=reaction_roles.message_checkpoints.stable_nonce(marker),
            author=SimpleNamespace(id=999),
            embeds=[],
        )

        async def history(*, limit):
            self.assertIsNone(limit)
            yield impostor

        channel = SimpleNamespace(history=history)
        guild = SimpleNamespace(
            get_channel=lambda channel_id: channel if channel_id == 200 else None,
            me=SimpleNamespace(id=100),
        )
        record = RuntimeRecord(
            kind=reaction_roles._PUBLISH_RUNTIME_KIND,
            guild_id=42,
            external_id="operation-one",
            payload={"panel": {"destination_channel_id": 200}},
            status="prepared",
            updated_at="now",
        )

        with patch.object(
            reaction_roles.discord.abc,
            "Messageable",
            type(channel),
        ):
            resolved = await reaction_roles._resolve_pending_publish_message(guild, record)

        self.assertIsNone(resolved)

    async def test_persisted_message_id_is_authoritative_without_checkpoint(self) -> None:
        panel_message = SimpleNamespace(
            id=300,
            content=None,
            nonce=None,
            author=SimpleNamespace(id=100),
            embeds=[SimpleNamespace(footer=SimpleNamespace(text=None))],
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=panel_message))
        guild = SimpleNamespace(
            get_channel=lambda channel_id: channel if channel_id == 200 else None,
            me=SimpleNamespace(id=100),
        )
        record = RuntimeRecord(
            kind=reaction_roles._PUBLISH_RUNTIME_KIND,
            guild_id=42,
            external_id="operation-one",
            payload={
                "panel": {
                    "panel_channel_id": 200,
                    "panel_message_id": 300,
                }
            },
            status="ready_to_commit",
            updated_at="now",
        )

        with patch.object(
            reaction_roles.discord.abc,
            "Messageable",
            type(channel),
        ):
            resolved = await reaction_roles._resolve_pending_publish_message(guild, record)

        self.assertIs(panel_message, resolved)
        channel.fetch_message.assert_awaited_once_with(300)

    async def test_persisted_message_id_rejects_another_author_when_bot_is_known(
        self,
    ) -> None:
        panel_message = SimpleNamespace(id=300, author=SimpleNamespace(id=999))
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=panel_message))
        guild = SimpleNamespace(
            get_channel=lambda channel_id: channel if channel_id == 200 else None,
            me=SimpleNamespace(id=100),
        )
        record = RuntimeRecord(
            kind=reaction_roles._PUBLISH_RUNTIME_KIND,
            guild_id=42,
            external_id="operation-one",
            payload={
                "panel": {
                    "panel_channel_id": 200,
                    "panel_message_id": 300,
                }
            },
            status="ready_to_commit",
            updated_at="now",
        )

        with patch.object(
            reaction_roles.discord.abc,
            "Messageable",
            type(channel),
        ):
            resolved = await reaction_roles._resolve_pending_publish_message(guild, record)

        self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()
