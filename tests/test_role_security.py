from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from src.realm_protector.infrastructure import role_security_store
from src.realm_protector.services import role_security


def _guild(guild_id: int = 42):
    return SimpleNamespace(id=guild_id, me=None, roles=[])


def _role(role_id: int, name: str, guild):
    role = SimpleNamespace(
        id=role_id,
        name=name,
        guild=guild,
        managed=False,
        permissions=SimpleNamespace(),
        is_default=lambda: False,
        is_assignable=lambda: True,
    )
    return role


class RoleSecurityPolicyTests(TestCase):
    def test_self_assignment_accepts_bot_privileged_role(self) -> None:
        guild = _guild()
        privileged = _role(10, "Staff", guild)
        state = role_security.RoleSecurityState(
            privileged_id_sources={10: frozenset({"ticket management"})},
        )

        error = role_security.self_assignment_error(privileged, guild, state)

        self.assertIsNone(error)

    def test_privileged_assignment_accepts_self_assignable_role(self) -> None:
        guild = _guild()
        self_assignable = _role(20, "Reaction Role", guild)
        state = role_security.RoleSecurityState(
            self_assignable_id_sources={20: frozenset({"reaction panel"})},
        )

        error = role_security.privileged_assignment_error(
            self_assignable,
            guild,
            state,
        )

        self.assertIsNone(error)

    def test_pending_setup_state_allows_same_role_in_both_directions(self) -> None:
        guild = _guild()
        shared = _role(30, "Shared", guild)
        state = role_security.RoleSecurityState().extended(
            privileged_ids=[30],
            self_assignable_ids=[30],
        )

        self.assertIsNone(role_security.self_assignment_error(shared, guild, state))
        self.assertIsNone(role_security.privileged_assignment_error(shared, guild, state))

    def test_runtime_authorization_accepts_overlap(self) -> None:
        guild = _guild()
        shared = _role(40, "Shared", guild)
        member = SimpleNamespace(guild=guild, roles=[shared])
        state = role_security.RoleSecurityState(
            privileged_id_sources={40: frozenset({"Economy Manager role"})},
            self_assignable_id_sources={40: frozenset({"reaction panel"})},
        )

        self.assertTrue(
            role_security.member_has_safe_privileged_role(
                member,
                role_ids=[40],
                state=state,
            )
        )

    def test_runtime_authorization_rejects_unconfigured_legacy_default(self) -> None:
        guild = _guild()
        caller = _role(41, "Caller", guild)
        member = SimpleNamespace(guild=guild, roles=[caller])

        self.assertFalse(
            role_security.member_has_safe_privileged_role(
                member,
                role_names=["Caller"],
                state=role_security.RoleSecurityState(),
            )
        )

    def test_legacy_name_overlap_can_authorize(self) -> None:
        guild = _guild()
        shared = _role(50, "Caller", guild)
        member = SimpleNamespace(guild=guild, roles=[shared])
        state = role_security.RoleSecurityState(
            privileged_name_sources={"caller": frozenset({"legacy Caller role"})},
            self_assignable_id_sources={50: frozenset({"reaction panel"})},
        )

        self.assertTrue(
            role_security.member_has_safe_privileged_role(
                member,
                role_names=["Caller"],
                state=state,
            ),
        )


class RoleSecurityCollectionTests(TestCase):
    def test_role_sources_use_guild_rows_and_keep_disabled_tombstones(self) -> None:
        rows = {
            "tickets": {
                "disabled": True,
                "panels": {
                    "ticket-old": {
                        "active": False,
                        "management_role_ids": [20],
                    }
                },
            },
            "reaction_roles": {
                "disabled": True,
                "panels": {
                    "reaction-old": {
                        "disabled": True,
                        "reactions": [{"role_id": "30"}],
                    }
                },
            },
            "objectives": {
                "disabled": True,
                "objectives": [{"id": "objective-old", "notify_role_id": 40}],
            },
        }

        with patch.object(
            role_security_store.document_store,
            "get_mapping_entry",
            side_effect=lambda namespace, guild_id: rows.get(namespace) if guild_id == 42 else None,
        ) as get_entry:
            ticket_sources = role_security_store.get_ticket_management_role_sources(42)
            reaction_sources = role_security_store.get_reaction_role_sources(42)
            objective_sources = role_security_store.get_objective_notification_role_sources(42)

        self.assertEqual({20: {"ticket management panel ticket-old"}}, ticket_sources)
        self.assertEqual({30: {"reaction panel reaction-old"}}, reaction_sources)
        self.assertEqual(
            {40: {"objective notification objective-old"}},
            objective_sources,
        )
        self.assertEqual(
            [
                ("tickets", 42),
                ("reaction_roles", 42),
                ("objectives", 42),
            ],
            [call.args for call in get_entry.call_args_list],
        )

    def test_collects_every_privileged_and_self_assignment_source(self) -> None:
        with (
            patch.object(
                role_security.guild_settings,
                "get_configuration",
                return_value=SimpleNamespace(
                    caller_role_ids=(1,),
                    caller_role_names=("Caller",),
                    economy_manager_role_ids=(),
                    economy_manager_role_names=("Treasury",),
                    member_role_id=4,
                    member_role_name="Member",
                ),
            ) as get_configuration,
            patch.object(
                role_security.role_security_store,
                "get_ticket_management_role_sources",
                return_value={2: {"ticket management panel inactive-too"}},
            ),
            patch.object(
                role_security.role_security_store,
                "get_reaction_role_sources",
                return_value={2: {"reaction panel current"}, 3: {"reaction panel other"}},
            ),
            patch.object(
                role_security.role_security_store,
                "get_objective_notification_role_sources",
                return_value={5: {"objective notification"}},
            ),
        ):
            state = role_security.collect_role_security_state(42)

        self.assertEqual(frozenset({1, 2}), state.privileged_ids)
        self.assertEqual(frozenset({"treasury"}), state.privileged_legacy_names)
        self.assertEqual(frozenset({2, 3, 4, 5}), state.self_assignable_ids)
        self.assertIn("ticket management panel inactive-too", state.privileged_id_sources[2])
        self.assertIn("reaction panel current", state.self_assignable_id_sources[2])
        get_configuration.assert_called_once_with(42)
