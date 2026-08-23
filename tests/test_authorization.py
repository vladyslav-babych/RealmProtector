import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.realm_protector.bot import composition, economy_commands
from src.realm_protector.services import authorization


def _member_with_role(role_id: int, role_name: str):
    role = SimpleNamespace(
        id=role_id,
        name=role_name,
        permissions=SimpleNamespace(administrator=False),
    )
    return SimpleNamespace(
        roles=[role],
        guild_permissions=SimpleNamespace(administrator=False),
    )


class CallerAuthorizationTests(unittest.TestCase):
    def test_configured_role_ids_take_precedence_over_legacy_role_names(self) -> None:
        legacy_name_match = _member_with_role(999, "Caller")
        renamed_role_id_match = _member_with_role(123, "Renamed Caller")

        self.assertFalse(
            composition.has_caller_role(
                legacy_name_match,
                ["Caller"],
                caller_role_ids=[123],
            )
        )
        self.assertTrue(
            composition.has_caller_role(
                renamed_role_id_match,
                ["Caller"],
                caller_role_ids=[123],
            )
        )

    def test_legacy_role_names_remain_a_fallback_when_no_ids_are_configured(
        self,
    ) -> None:
        member = _member_with_role(999, "CALLER")

        self.assertTrue(composition.has_caller_role(member, ["Caller"], []))


class EconomyAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_role_ids_take_precedence_over_legacy_role_names(
        self,
    ) -> None:
        legacy_name_match = _member_with_role(999, "Economy Manager")
        renamed_role_id_match = _member_with_role(321, "Treasury")

        with (
            patch.object(
                economy_commands,
                "is_admin",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                economy_commands.guild_settings,
                "get_economy_manager_role_ids",
                return_value=[321],
            ),
            patch.object(
                economy_commands.guild_settings,
                "get_economy_manager_roles",
                return_value=["Economy Manager"],
            ) as get_legacy_names,
        ):
            self.assertFalse(
                await economy_commands._has_economy_access(
                    legacy_name_match,
                    guild_id=10,
                )
            )
            self.assertTrue(
                await economy_commands._has_economy_access(
                    renamed_role_id_match,
                    guild_id=10,
                )
            )

        get_legacy_names.assert_not_called()

    async def test_legacy_economy_name_is_used_only_without_configured_ids(
        self,
    ) -> None:
        member = _member_with_role(999, "Economy Manager")

        with (
            patch.object(
                economy_commands,
                "is_admin",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                economy_commands.guild_settings,
                "get_economy_manager_role_ids",
                return_value=[],
            ),
            patch.object(
                economy_commands.guild_settings,
                "get_economy_manager_roles",
                return_value=["Economy Manager"],
            ),
        ):
            self.assertTrue(await economy_commands._has_economy_access(member, guild_id=10))


class AutomaticRoleSafetyTests(unittest.TestCase):
    def _role(self, guild, **enabled_permissions):
        return SimpleNamespace(
            id=100,
            name="Member",
            guild=guild,
            managed=False,
            permissions=SimpleNamespace(**enabled_permissions),
            is_default=lambda: False,
            is_assignable=lambda: True,
        )

    def test_self_assignment_rejects_a_role_from_another_guild(self) -> None:
        guild = SimpleNamespace(id=10, me=None)
        role = self._role(SimpleNamespace(id=11), send_messages=True)

        error = authorization.automatic_role_assignment_error(role, guild)

        self.assertIn("another server", error or "")

    def test_permission_allowlist_fails_closed_for_future_permissions(self) -> None:
        guild = SimpleNamespace(id=10, me=None)
        role = self._role(guild, future_manage_everything=True)

        error = authorization.automatic_role_assignment_error(role, guild)

        self.assertIn("Future Manage Everything", error or "")

    def test_reviewed_ordinary_member_permissions_remain_assignable(self) -> None:
        guild = SimpleNamespace(id=10, me=None)
        role = self._role(
            guild,
            read_messages=True,
            send_messages=True,
            use_application_commands=True,
        )

        self.assertIsNone(authorization.automatic_role_assignment_error(role, guild))

    def test_mention_everyone_and_voice_status_permissions_are_assignable(self) -> None:
        guild = SimpleNamespace(id=10, me=None)
        role = self._role(
            guild,
            mention_everyone=True,
            set_voice_channel_status=True,
        )

        self.assertIsNone(authorization.automatic_role_assignment_error(role, guild))


if __name__ == "__main__":
    unittest.main()
