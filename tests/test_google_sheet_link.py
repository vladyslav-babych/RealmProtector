import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.realm_protector.bot import google_sheet_link
from src.realm_protector.services import guild_lifecycle


class GoogleSheetLinkLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_wizard_cannot_recreate_credentials(self) -> None:
        class FakeMember:
            def __init__(self, member_id: int) -> None:
                self.id = member_id

        guild = SimpleNamespace(id=515151)
        state = {
            "credentials_json": '{"private_key":"x"}',
            "credentials_file_name_preview": "preview.json",
            "google_sheet_name": "Realm",
            "google_worksheet_name": "Players",
            "lootsplit_history_worksheet_name": "Lootsplit History",
            "balance_history_worksheet_name": "Balance History",
        }
        with patch.object(
            google_sheet_link.guild_settings,
            "get_target_guild",
            return_value="Realm",
        ):
            view = google_sheet_link.GoogleSheetLinkStepView(
                guild,
                user_id=7,
                step=6,
                state=state,
            )
        guild_lifecycle.advance(guild.id)

        interaction = SimpleNamespace(
            user=FakeMember(7),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=None,
        )
        finish_button = next(
            child
            for child in view.children
            if isinstance(child, google_sheet_link.GoogleSheetLinkFinishButton)
        )

        with (
            patch.object(google_sheet_link.discord, "Member", FakeMember),
            patch.object(
                google_sheet_link.authorization,
                "is_admin",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                google_sheet_link.guild_settings,
                "get_target_guild",
                return_value="Realm",
            ),
            patch.object(
                google_sheet_link.credential_store,
                "link_google_sheet_credentials",
            ) as link_credentials,
        ):
            await finish_button.callback(interaction)

        link_credentials.assert_not_called()
        interaction.followup.send.assert_awaited_once()
        self.assertIn(
            "configuration changed",
            interaction.followup.send.await_args.args[0].lower(),
        )
        self.assertEqual("", view.state["credentials_json"])


if __name__ == "__main__":
    unittest.main()
