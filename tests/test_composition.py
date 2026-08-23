import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.realm_protector.bot import composition, message_checkpoints
from src.realm_protector.bot.composition import (
    find_first_mention,
    find_role_index_by_number,
    has_caller_access,
    officer_forced_signout,
    parse_roles,
    sign_out_self,
    sign_up_user,
)


def _member(mention: str, *, role_name: str = "Member", administrator: bool = False):
    role = SimpleNamespace(
        name=role_name,
        permissions=SimpleNamespace(administrator=administrator),
    )
    member_id = int(mention.removeprefix("<@!").removeprefix("<@").removesuffix(">"))
    return SimpleNamespace(id=member_id, mention=mention, roles=[role], bot=False)


def _allowed_user_ids(allowed_mentions) -> list[int]:
    users = allowed_mentions.users
    return [user.id for user in users] if isinstance(users, list) else []


def _assert_safe_mentions(test_case, allowed_mentions, expected_ids: list[int]) -> None:
    test_case.assertEqual(expected_ids, _allowed_user_ids(allowed_mentions))
    test_case.assertFalse(allowed_mentions.roles)
    test_case.assertFalse(allowed_mentions.everyone)
    test_case.assertFalse(allowed_mentions.replied_user)


class CompositionParsingTests(unittest.TestCase):
    def test_parses_each_line_and_preserves_party_header(self) -> None:
        self.assertEqual(
            ["Party 1", "1. Tank", "2. Healer"],
            parse_roles("Party 1\n1. Tank\n2. Healer"),
        )
        self.assertEqual([], parse_roles(""))

    def test_role_number_matching_is_exact(self) -> None:
        roles = ["Party 1", "10. Battle Mount", "1. Tank"]

        self.assertEqual(2, find_role_index_by_number(roles, 1))
        self.assertEqual(1, find_role_index_by_number(roles, 10))
        self.assertIsNone(find_role_index_by_number(roles, 2))

    def test_finds_normal_and_nickname_discord_mentions(self) -> None:
        self.assertEqual("<@123>", find_first_mention("1. Tank <@123>"))
        self.assertEqual("<@!456>", find_first_mention("2. Healer <@!456>"))
        self.assertIsNone(find_first_mention("3. Support"))

    def test_caller_access_accepts_configured_role_case_insensitively_or_admin(self) -> None:
        caller = _member("<@1>", role_name="Shot Caller")
        administrator = _member("<@2>", administrator=True)
        regular_member = _member("<@3>")

        self.assertTrue(has_caller_access(caller, ["shot caller"]))
        self.assertTrue(has_caller_access(administrator, []))
        self.assertFalse(has_caller_access(regular_member, ["Caller"]))


class CompositionSignupTests(unittest.IsolatedAsyncioTestCase):
    async def test_signup_edit_allows_every_assigned_member_mention(self) -> None:
        member = _member("<@123>")
        message = SimpleNamespace(author=member, reply=AsyncMock())
        starter = SimpleNamespace(edit=AsyncMock())
        roles = ["Party 1", "1. Tank", "2. Healer <@456>"]

        await sign_up_user(message, roles, starter, 1)

        self.assertEqual("1. Tank <@123>", roles[1])
        starter.edit.assert_awaited_once()
        edit_call = starter.edit.await_args
        self.assertEqual(
            "Party 1\n1. Tank <@123>\n2. Healer <@456>",
            edit_call.kwargs["content"],
        )
        _assert_safe_mentions(self, edit_call.kwargs["allowed_mentions"], [123, 456])
        message.reply.assert_awaited_once()
        reply_call = message.reply.await_args
        self.assertEqual("<@123> was signed up as **Tank**", reply_call.args[0])
        _assert_safe_mentions(self, reply_call.kwargs["allowed_mentions"], [123])

    async def test_signup_does_not_overwrite_an_existing_claim(self) -> None:
        member = _member("<@123>")
        message = SimpleNamespace(author=member, reply=AsyncMock())
        starter = SimpleNamespace(edit=AsyncMock())
        roles = ["Party 1", "1. Tank <@999>"]

        await sign_up_user(message, roles, starter, 1)

        self.assertEqual("1. Tank <@999>", roles[1])
        starter.edit.assert_not_awaited()
        message.reply.assert_awaited_once()
        reply_call = message.reply.await_args
        self.assertEqual("This role is already taken.", reply_call.args[0])
        _assert_safe_mentions(self, reply_call.kwargs["allowed_mentions"], [])

    async def test_repeating_the_same_signup_is_idempotent(self) -> None:
        member = _member("<@123>")
        message = SimpleNamespace(author=member, reply=AsyncMock())
        starter = SimpleNamespace(edit=AsyncMock())
        roles = ["Party 1", "1. Tank <@123>"]

        await sign_up_user(message, roles, starter, 1)

        starter.edit.assert_not_awaited()
        message.reply.assert_awaited_once()
        reply_call = message.reply.await_args
        self.assertIn("already signed up for this role", reply_call.args[0].casefold())
        _assert_safe_mentions(self, reply_call.kwargs["allowed_mentions"], [])

    async def test_member_cannot_claim_a_second_role_in_the_same_party(self) -> None:
        member = _member("<@123>")
        message = SimpleNamespace(author=member, reply=AsyncMock())
        starter = SimpleNamespace(edit=AsyncMock())
        roles = ["Party 1", "1. Tank <@123>", "2. Healer"]

        await sign_up_user(message, roles, starter, 2)

        self.assertEqual(["Party 1", "1. Tank <@123>", "2. Healer"], roles)
        starter.edit.assert_not_awaited()
        message.reply.assert_awaited_once()
        reply_call = message.reply.await_args
        self.assertIn("sign out", reply_call.args[0].casefold())
        _assert_safe_mentions(self, reply_call.kwargs["allowed_mentions"], [])

    async def test_nickname_mention_is_the_same_member_for_idempotent_signup(self) -> None:
        member = _member("<@123>")
        message = SimpleNamespace(author=member, reply=AsyncMock())
        starter = SimpleNamespace(edit=AsyncMock())
        roles = ["Party 1", "1. Tank <@!123>"]

        await sign_up_user(message, roles, starter, 1)

        self.assertEqual(["Party 1", "1. Tank <@!123>"], roles)
        starter.edit.assert_not_awaited()
        message.reply.assert_awaited_once()
        reply_call = message.reply.await_args
        self.assertIn("already signed up for this role", reply_call.args[0].casefold())
        _assert_safe_mentions(self, reply_call.kwargs["allowed_mentions"], [])

    async def test_member_can_sign_out_their_own_claim(self) -> None:
        member = _member("<@123>")
        message = SimpleNamespace(author=member, reply=AsyncMock())
        starter = SimpleNamespace(edit=AsyncMock())
        roles = ["Party 1", "1. Tank <@123>", "2. Healer <@456>"]

        await sign_out_self(message, roles, starter)

        self.assertEqual("1. Tank", roles[1])
        self.assertEqual("2. Healer <@456>", roles[2])
        starter.edit.assert_awaited_once()
        edit_call = starter.edit.await_args
        self.assertEqual(
            "Party 1\n1. Tank\n2. Healer <@456>",
            edit_call.kwargs["content"],
        )
        _assert_safe_mentions(self, edit_call.kwargs["allowed_mentions"], [456])
        message.reply.assert_awaited_once()
        reply_call = message.reply.await_args
        self.assertEqual("<@123> was signed out from **Tank**", reply_call.args[0])
        _assert_safe_mentions(self, reply_call.kwargs["allowed_mentions"], [123])

    async def test_member_can_sign_out_a_nickname_mention_claim(self) -> None:
        member = _member("<@123>")
        message = SimpleNamespace(author=member, reply=AsyncMock())
        starter = SimpleNamespace(edit=AsyncMock())
        roles = ["Party 1", "1. Tank <@!123>", "2. Healer <@456>"]

        await sign_out_self(message, roles, starter)

        self.assertEqual("1. Tank", roles[1])
        self.assertEqual("2. Healer <@456>", roles[2])
        starter.edit.assert_awaited_once()
        edit_call = starter.edit.await_args
        self.assertEqual(
            "Party 1\n1. Tank\n2. Healer <@456>",
            edit_call.kwargs["content"],
        )
        _assert_safe_mentions(self, edit_call.kwargs["allowed_mentions"], [456])
        message.reply.assert_awaited_once()
        reply_call = message.reply.await_args
        self.assertIn("signed out from **Tank**", reply_call.args[0])
        _assert_safe_mentions(self, reply_call.kwargs["allowed_mentions"], [123])

    async def test_only_a_caller_or_admin_can_force_signout(self) -> None:
        regular = _member("<@123>")
        denied_message = SimpleNamespace(author=regular, reply=AsyncMock())
        denied_starter = SimpleNamespace(edit=AsyncMock())
        denied_roles = ["Party 1", "1. Tank <@999>"]

        await officer_forced_signout(
            denied_message,
            denied_roles,
            denied_starter,
            1,
            ["Caller"],
        )

        self.assertEqual("1. Tank <@999>", denied_roles[1])
        denied_starter.edit.assert_not_awaited()

        caller = _member("<@321>", role_name="Caller")
        allowed_message = SimpleNamespace(author=caller, reply=AsyncMock())
        allowed_starter = SimpleNamespace(edit=AsyncMock())
        allowed_roles = ["Party 1", "1. Tank <@999>"]

        await officer_forced_signout(
            allowed_message,
            allowed_roles,
            allowed_starter,
            1,
            ["Caller"],
        )

        self.assertEqual("1. Tank", allowed_roles[1])
        allowed_starter.edit.assert_awaited_once()
        allowed_message.reply.assert_awaited_once()
        reply_call = allowed_message.reply.await_args
        self.assertEqual("<@999> was signed out from **Tank**", reply_call.args[0])
        _assert_safe_mentions(self, reply_call.kwargs["allowed_mentions"], [999])

    async def test_composition_replies_allow_only_explicit_user_mentions(self) -> None:
        allowed_mentions = composition.allowed_user_mentions([123, "456", 123, 0, "invalid"])

        _assert_safe_mentions(self, allowed_mentions, [123, 456])

    async def test_caller_cannot_force_target_into_a_second_party_role(self) -> None:
        class MutableStarter:
            id = 987_653

            def __init__(self) -> None:
                self.content = "Party 1\n1. Tank <@123>\n2. Healer"
                self.author = SimpleNamespace(id=42)

            async def edit(self, *, content: str) -> None:
                self.content = content

        starter = MutableStarter()
        channel = SimpleNamespace(starter_message=starter)
        channel.fetch_message = AsyncMock(return_value=starter)
        caller = _member("<@321>", role_name="Caller")
        target = _member("<@123>")
        guild = SimpleNamespace(
            id=10,
            me=SimpleNamespace(id=42),
            get_member=lambda user_id: target if user_id == target.id else None,
        )
        message = SimpleNamespace(
            author=caller,
            channel=channel,
            content="<@123> 2",
            guild=guild,
            reply=AsyncMock(),
        )

        with (
            patch.object(composition, "is_party_thread", return_value=True),
            patch.object(
                composition.guild_settings,
                "get_caller_roles",
                return_value=["Caller"],
            ),
            patch.object(
                composition.guild_settings,
                "get_caller_role_ids",
                return_value=[],
            ),
        ):
            await composition.on_message_in_thread(message)

        self.assertEqual("Party 1\n1. Tank <@123>\n2. Healer", starter.content)
        message.reply.assert_awaited_once()
        reply_call = message.reply.await_args
        self.assertIn("already signed up for another role", reply_call.args[0].casefold())
        _assert_safe_mentions(self, reply_call.kwargs["allowed_mentions"], [])

    async def test_caller_can_force_signup_a_mentioned_member_on_cache_miss(self) -> None:
        class MutableStarter:
            id = 987_650

            def __init__(self) -> None:
                self.content = "Party 1\n1. Tank\n2. Healer"
                self.author = SimpleNamespace(id=42)
                self.allowed_mentions = None

            async def edit(self, *, content: str, allowed_mentions=None) -> None:
                self.content = content
                self.allowed_mentions = allowed_mentions

        starter = MutableStarter()
        channel = SimpleNamespace(starter_message=starter)
        channel.fetch_message = AsyncMock(return_value=starter)
        caller = _member("<@321>", role_name="Caller")
        target = _member("<@123>")
        guild = SimpleNamespace(
            id=10,
            me=SimpleNamespace(id=42),
            get_member=lambda _user_id: None,
        )
        message = SimpleNamespace(
            author=caller,
            channel=channel,
            content="<@123> 2",
            guild=guild,
            mentions=[target],
            reply=AsyncMock(),
        )

        with (
            patch.object(composition, "is_party_thread", return_value=True),
            patch.object(
                composition.guild_settings,
                "get_caller_roles",
                return_value=["Caller"],
            ),
            patch.object(
                composition.guild_settings,
                "get_caller_role_ids",
                return_value=[],
            ),
        ):
            await composition.on_message_in_thread(message)

        self.assertEqual("Party 1\n1. Tank\n2. Healer <@123>", starter.content)
        _assert_safe_mentions(self, starter.allowed_mentions, [123])
        message.reply.assert_awaited_once()
        self.assertIn("was signed up as **Healer**", message.reply.await_args.args[0])
        _assert_safe_mentions(
            self,
            message.reply.await_args.kwargs["allowed_mentions"],
            [123],
        )

    async def test_type_21_wrapper_resolves_parent_starter_and_signup_works(self) -> None:
        class FakeThread:
            pass

        class FakeParent(discord.abc.Messageable):
            pass

        class MutableStarter:
            id = 987_652
            type = discord.MessageType.default

            def __init__(self) -> None:
                self.content = "Party 1\n1. Tank\n2. Healer"
                self.author = SimpleNamespace(id=42)

            async def edit(self, *, content: str, allowed_mentions=None) -> None:
                self.content = content

        starter = MutableStarter()
        parent = FakeParent()
        parent.id = 700
        parent.fetch_message = AsyncMock(return_value=starter)
        wrapper = SimpleNamespace(
            id=starter.id,
            type=discord.MessageType.thread_starter_message,
            content="",
            author=SimpleNamespace(id=42),
            reference=SimpleNamespace(
                channel_id=parent.id,
                message_id=starter.id,
                resolved=None,
            ),
            edit=AsyncMock(),
        )
        thread = FakeThread()
        thread.id = starter.id
        thread.name = "Party 1 thread"
        thread.parent = parent
        thread.parent_id = parent.id
        thread.starter_message = wrapper
        thread.fetch_message = AsyncMock(return_value=wrapper)
        guild = SimpleNamespace(
            id=10,
            me=SimpleNamespace(id=42),
            get_channel=lambda channel_id: parent if channel_id == parent.id else None,
            fetch_channel=AsyncMock(return_value=parent),
        )
        thread.guild = guild
        member = _member("<@123>")
        message = SimpleNamespace(
            author=member,
            channel=thread,
            content="1",
            guild=guild,
            reply=AsyncMock(),
        )

        with (
            patch.object(composition.discord, "Thread", FakeThread),
            patch.object(
                composition.guild_settings,
                "get_caller_roles",
                return_value=[],
            ),
            patch.object(
                composition.guild_settings,
                "get_caller_role_ids",
                return_value=[],
            ),
        ):
            await composition.on_message_in_thread(message)

        self.assertEqual("Party 1\n1. Tank <@123>\n2. Healer", starter.content)
        parent.fetch_message.assert_awaited_with(starter.id)
        wrapper.edit.assert_not_awaited()
        message.reply.assert_awaited_once()
        self.assertIn("was signed up as **Tank**", message.reply.await_args.args[0])

    async def test_simultaneous_messages_cannot_both_claim_the_same_slot(self) -> None:
        class MutableStarter:
            id = 987_654

            def __init__(self) -> None:
                self.content = "Party 1\n1. Tank"
                self.author = SimpleNamespace(id=42)

            async def edit(self, *, content: str, allowed_mentions=None) -> None:
                self.content = content

        starter = MutableStarter()
        channel = SimpleNamespace(starter_message=starter)

        async def fetch_message(message_id: int):
            self.assertEqual(starter.id, message_id)
            return starter

        channel.fetch_message = fetch_message
        guild = SimpleNamespace(id=10, me=SimpleNamespace(id=42))
        first = SimpleNamespace(
            author=SimpleNamespace(id=1, mention="<@1>", bot=False, roles=[]),
            channel=channel,
            content="1",
            guild=guild,
            reply=AsyncMock(),
        )
        second = SimpleNamespace(
            author=SimpleNamespace(id=2, mention="<@2>", bot=False, roles=[]),
            channel=channel,
            content="1",
            guild=guild,
            reply=AsyncMock(),
        )

        with (
            patch.object(composition, "is_party_thread", return_value=True),
            patch.object(
                composition.guild_settings,
                "get_caller_roles",
                return_value=[],
            ),
        ):
            await asyncio.gather(
                composition.on_message_in_thread(first),
                composition.on_message_in_thread(second),
            )

        claimed_mentions = [mention for mention in ("<@1>", "<@2>") if mention in starter.content]
        self.assertEqual(1, len(claimed_mentions))
        replies = [call.args[0] for call in first.reply.await_args_list]
        replies.extend(call.args[0] for call in second.reply.await_args_list)
        self.assertEqual(1, sum("was signed up" in reply for reply in replies))
        self.assertEqual(1, sum(reply == "This role is already taken." for reply in replies))

    async def test_simultaneous_same_member_signups_leave_only_one_claim(self) -> None:
        class MutableStarter:
            id = 987_651

            def __init__(self) -> None:
                self.content = "Party 1\n1. Tank\n2. Healer"
                self.author = SimpleNamespace(id=42)

            async def edit(self, *, content: str, allowed_mentions=None) -> None:
                self.content = content

        starter = MutableStarter()
        channel = SimpleNamespace(starter_message=starter)
        channel.fetch_message = AsyncMock(return_value=starter)
        guild = SimpleNamespace(id=10, me=SimpleNamespace(id=42))
        member = SimpleNamespace(id=123, mention="<@123>", bot=False, roles=[])
        first = SimpleNamespace(
            author=member,
            channel=channel,
            content="1",
            guild=guild,
            reply=AsyncMock(),
        )
        second = SimpleNamespace(
            author=member,
            channel=channel,
            content="2",
            guild=guild,
            reply=AsyncMock(),
        )

        with (
            patch.object(composition, "is_party_thread", return_value=True),
            patch.object(
                composition.guild_settings,
                "get_caller_roles",
                return_value=[],
            ),
            patch.object(
                composition.guild_settings,
                "get_caller_role_ids",
                return_value=[],
            ),
        ):
            await asyncio.gather(
                composition.on_message_in_thread(first),
                composition.on_message_in_thread(second),
            )

        self.assertEqual(1, starter.content.count(member.mention))
        replies = [call.args[0] for call in first.reply.await_args_list]
        replies.extend(call.args[0] for call in second.reply.await_args_list)
        self.assertEqual(1, sum("was signed up" in reply for reply in replies))
        self.assertEqual(1, sum("sign out" in reply.casefold() for reply in replies))


class CompositionReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_party_is_fetched_on_cache_miss_and_restored(self) -> None:
        class FakeThread:
            pass

        starter = SimpleNamespace(
            id=101,
            author=SimpleNamespace(id=42),
            content="stale or malformed Discord content",
            edit=AsyncMock(),
        )
        thread = FakeThread()
        thread.id = 202
        thread.name = "Party 1 thread"
        thread.starter_message = None
        thread.fetch_message = AsyncMock(return_value=starter)
        guild = SimpleNamespace(
            id=303,
            threads=[],
            get_thread=lambda _channel_id: None,
            get_channel=lambda _channel_id: None,
            fetch_channel=AsyncMock(return_value=thread),
        )
        record = SimpleNamespace(
            external_id="101",
            status="active",
            payload={
                "starter_message_id": 101,
                "thread_id": 202,
                "content": "Party 1\n1. Tank",
            },
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=42), guilds=[guild])

        with (
            patch.object(composition.discord, "Thread", FakeThread),
            patch.object(
                composition.runtime_state,
                "list_records",
                return_value=[record],
            ),
        ):
            await composition.reconcile_compositions(bot)

        guild.fetch_channel.assert_awaited_once_with(202)
        thread.fetch_message.assert_awaited_once_with(101)
        starter.edit.assert_awaited_once()
        restored = starter.edit.await_args.kwargs
        self.assertEqual("Party 1\n1. Tank", restored["content"])
        _assert_safe_mentions(self, restored["allowed_mentions"], [])

    async def test_active_party_legacy_footer_is_cleaned_without_creation_record(self) -> None:
        class FakeThread:
            pass

        marker_embed = discord.Embed()
        marker_embed.set_footer(
            text=composition._creation_marker("lost-operation", 0),
        )
        starter = SimpleNamespace(
            id=101,
            author=SimpleNamespace(id=42),
            content="Party 1\n1. Tank",
            embeds=[marker_embed],
            edit=AsyncMock(),
        )
        thread = FakeThread()
        thread.id = 202
        thread.name = "Party 1 thread"
        thread.starter_message = starter
        guild = SimpleNamespace(
            id=303,
            threads=[],
            get_thread=lambda _channel_id: thread,
            get_channel=lambda _channel_id: None,
        )
        record = SimpleNamespace(
            external_id="101",
            status="active",
            payload={
                "starter_message_id": 101,
                "thread_id": 202,
                "content": "Party 1\n1. Tank",
            },
        )

        with patch.object(composition.discord, "Thread", FakeThread):
            await composition._reconcile_persisted_composition(guild, record, 42)

        starter.edit.assert_awaited_once_with(embeds=[])

    async def test_type_21_wrapper_reconciliation_restores_parent_starter(self) -> None:
        class FakeThread:
            pass

        class FakeParent(discord.abc.Messageable):
            pass

        starter_id = 101
        starter = SimpleNamespace(
            id=starter_id,
            type=discord.MessageType.default,
            author=SimpleNamespace(id=42),
            content="stale parent content",
            embeds=[],
            edit=AsyncMock(),
        )
        parent = FakeParent()
        parent.id = 201
        parent.fetch_message = AsyncMock(return_value=starter)
        wrapper = SimpleNamespace(
            id=starter_id,
            type=discord.MessageType.thread_starter_message,
            author=SimpleNamespace(id=42),
            content="",
            reference=SimpleNamespace(
                channel_id=parent.id,
                message_id=starter_id,
                resolved=None,
            ),
            edit=AsyncMock(),
        )
        thread = FakeThread()
        thread.id = starter_id
        thread.name = "Party 1 thread"
        thread.parent = parent
        thread.parent_id = parent.id
        thread.starter_message = wrapper
        thread.fetch_message = AsyncMock(return_value=wrapper)
        guild = SimpleNamespace(
            id=303,
            threads=[],
            get_thread=lambda channel_id: thread if channel_id == starter_id else None,
            get_channel=lambda channel_id: parent if channel_id == parent.id else None,
            fetch_channel=AsyncMock(return_value=parent),
        )
        thread.guild = guild
        record = SimpleNamespace(
            external_id=str(starter_id),
            status="active",
            payload={
                "starter_message_id": starter_id,
                "thread_id": starter_id,
                "content": "Party 1\n1. Tank",
            },
        )

        with patch.object(composition.discord, "Thread", FakeThread):
            await composition._reconcile_persisted_composition(guild, record, 42)

        parent.fetch_message.assert_awaited_with(starter_id)
        wrapper.edit.assert_not_awaited()
        starter.edit.assert_awaited_once()
        restored = starter.edit.await_args.kwargs
        self.assertEqual("Party 1\n1. Tank", restored["content"])
        _assert_safe_mentions(self, restored["allowed_mentions"], [])

    async def test_missing_persisted_thread_is_marked_without_recreation(self) -> None:
        class FakeNotFound(Exception):
            pass

        record = SimpleNamespace(
            external_id="101",
            status="active",
            payload={"starter_message_id": 101, "thread_id": 202, "content": "Party 1"},
        )
        guild = SimpleNamespace(
            id=303,
            threads=[],
            get_thread=lambda _channel_id: None,
            get_channel=lambda _channel_id: None,
            fetch_channel=AsyncMock(side_effect=FakeNotFound()),
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=42), guilds=[guild])

        with (
            patch.object(composition.discord, "NotFound", FakeNotFound),
            patch.object(
                composition.runtime_state,
                "list_records",
                return_value=[record],
            ),
            patch.object(composition.runtime_state, "set_status") as set_status,
        ):
            await composition.reconcile_compositions(bot)

        set_status.assert_called_once_with(
            composition._RUNTIME_KIND,
            303,
            "101",
            "missing",
        )


class CompositionCreationCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_message_id_is_authoritative_without_checkpoint(self) -> None:
        expected = SimpleNamespace(id=101, content="Party 1", embeds=[], nonce=None)
        channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=expected),
            history=MagicMock(),
        )

        resolved = await composition._find_pending_party_message(
            channel,
            "operation-one",
            0,
            101,
        )

        self.assertIs(expected, resolved)
        channel.history.assert_not_called()

    async def test_history_recovery_ignores_another_authors_checkpoint(self) -> None:
        marker = composition._creation_marker("operation-one", 0)
        spoof = SimpleNamespace(
            author=SimpleNamespace(id=999),
            content=message_checkpoints.content_with_checkpoint("Party 1", marker),
            embeds=[],
            nonce=None,
        )
        expected = SimpleNamespace(
            author=SimpleNamespace(id=100),
            content=message_checkpoints.content_with_checkpoint("Party 1", marker),
            embeds=[],
            nonce=None,
        )

        async def history(*, limit):
            self.assertEqual(composition._CREATION_HISTORY_LIMIT, limit)
            yield spoof
            yield expected

        channel = SimpleNamespace(
            guild=SimpleNamespace(me=SimpleNamespace(id=100)),
            history=history,
        )

        resolved = await composition._find_pending_party_message(
            channel,
            "operation-one",
            0,
            None,
        )

        self.assertIs(expected, resolved)

    async def test_new_party_checkpoint_is_removed_after_message_id_is_saved(self) -> None:
        events: list[str] = []
        desired_content = "Party 1\n1. Tank"

        class FakeThread:
            id = 202

        class MutableMessage:
            id = 101

            def __init__(self, content: str, guild, channel) -> None:
                self.content = content
                self.embeds: list[discord.Embed] = []
                self.guild = guild
                self.channel = channel
                self.thread = FakeThread()

            async def edit(self, **options):
                events.append("edit")
                if "content" in options:
                    self.content = str(options["content"] or "")
                if "embeds" in options:
                    self.embeds = list(options["embeds"])
                return self

        async def empty_history(*, limit):
            self.assertEqual(composition._CREATION_HISTORY_LIMIT, limit)
            if False:
                yield None

        guild = SimpleNamespace(id=303)
        destination = SimpleNamespace(id=404, history=empty_history)
        sent: dict[str, object] = {}

        async def send_message(content: str, **options):
            events.append("send")
            sent.update({"content": content, **options})
            message = MutableMessage(content, guild, destination)
            sent["message"] = message
            return message

        saved: list[tuple[str, dict]] = []

        def save_creation(_guild_id, external_id, payload, *, status):
            events.append(status)
            snapshot = copy.deepcopy(payload)
            saved.append((status, snapshot))
            return SimpleNamespace(external_id=external_id, payload=snapshot, status=status)

        record = SimpleNamespace(
            external_id="operation-one",
            payload={
                "destination_channel_id": destination.id,
                "parties": [{"content": desired_content, "thread_name": "Party 1 thread"}],
            },
        )

        with (
            patch.object(composition.discord, "Thread", FakeThread),
            patch.object(composition, "_save_creation_record", side_effect=save_creation),
            patch.object(composition, "_persist_party_message"),
        ):
            completed = await composition._resume_composition_creation_locked(
                None,
                guild,
                record,
                destination_channel=destination,
                send_message=send_message,
            )

        marker = composition._creation_marker("operation-one", 0)
        self.assertTrue(completed)
        self.assertNotIn(marker, str(sent["content"]))
        self.assertEqual(message_checkpoints.stable_nonce(marker), sent["nonce"])
        self.assertNotIn("embed", sent)
        self.assertNotIn("embeds", sent)
        self.assertEqual(["send", "message_ready", "edit"], events[:3])
        self.assertEqual(desired_content, sent["message"].content)
        self.assertEqual(desired_content, saved[-1][1]["parties"][0]["content"])
        self.assertTrue(saved[-1][1][composition._MESSAGE_CHECKPOINT_CLEANUP_FIELD])
        self.assertEqual("completed", saved[-1][0])

    async def test_completed_legacy_footer_is_removed_and_flagged(self) -> None:
        marker = composition._creation_marker("operation-one", 0)
        legacy_embed = discord.Embed()
        legacy_embed.set_footer(text=marker)
        message = SimpleNamespace(
            id=101,
            content="Party 1\n1. Tank",
            embeds=[legacy_embed],
            nonce=None,
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=message),
            history=MagicMock(),
        )
        guild = SimpleNamespace(id=303)
        record = SimpleNamespace(
            external_id="operation-one",
            payload={
                "destination_channel_id": 404,
                "parties": [{"content": "Party 1\n1. Tank", "starter_message_id": 101}],
            },
        )

        with (
            patch.object(composition, "_fetch_guild_channel", new=AsyncMock(return_value=channel)),
            patch.object(composition, "_save_creation_record") as save_creation,
        ):
            cleaned = await composition._clean_completed_composition_creation(guild, record)

        self.assertTrue(cleaned)
        message.edit.assert_awaited_once_with(embeds=[])
        self.assertEqual("Party 1\n1. Tank", message.content)
        channel.history.assert_not_called()
        saved_payload = save_creation.call_args.args[2]
        self.assertTrue(saved_payload[composition._MESSAGE_CHECKPOINT_CLEANUP_FIELD])
        self.assertEqual("completed", save_creation.call_args.kwargs["status"])


if __name__ == "__main__":
    unittest.main()
