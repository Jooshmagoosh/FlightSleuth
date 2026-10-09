import os
import unittest
from pathlib import Path
from unittest import mock

from flightsleuth import config
from flightsleuth.errors import UserError
from tests.fakes import install


class KeyTest(unittest.TestCase):
    def setUp(self):
        # install() also moves into an empty folder, so no real .env is in reach.
        self.home = Path(install(self))
        self.enterContext(mock.patch.dict(os.environ, {config.KEY_VAR: ""}))

    def test_missing_key(self):
        self.assertIsNone(config.find_api_key())
        with self.assertRaises(UserError):
            config.load_api_key()

    def test_environment_wins(self):
        (self.home / ".env").write_text("SERPAPI_API_KEY=from-file\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {config.KEY_VAR: " from-env "}):
            self.assertEqual(config.load_api_key(), "from-env")

    def test_key_is_read_from_a_dotenv_file(self):
        (self.home / ".env").write_text('# comment\nSERPAPI_API_KEY="from-file"\n', encoding="utf-8")
        self.assertEqual(config.load_api_key(), "from-file")

    def test_blank_key_in_the_file_counts_as_missing(self):
        (self.home / ".env").write_text("SERPAPI_API_KEY=\n", encoding="utf-8")
        self.assertIsNone(config.find_api_key())

    def test_saved_key_is_found_again(self):
        path = config.save_api_key("  pasted-key\n")
        self.assertEqual(path.read_text(encoding="utf-8"), "SERPAPI_API_KEY=pasted-key\n")
        self.assertEqual(config.load_api_key(), "pasted-key")

    def test_saving_replaces_the_old_key_and_keeps_other_lines(self):
        (self.home / ".env").write_text("# my notes\nSERPAPI_API_KEY=old\nOTHER=1\n", encoding="utf-8")
        config.save_api_key("new")
        self.assertEqual(
            (self.home / ".env").read_text(encoding="utf-8"), "# my notes\nOTHER=1\nSERPAPI_API_KEY=new\n"
        )

    def test_key_goes_to_flightsleuths_own_folder_when_there_is_no_local_file(self):
        elsewhere = self.home / "elsewhere"
        with mock.patch.dict(os.environ, {config.HOME_VAR: str(elsewhere)}):
            self.assertEqual(config.save_api_key("k"), elsewhere / ".env")
            self.assertEqual(config.load_api_key(), "k")


class SettingsTest(unittest.TestCase):
    """The alert topic and the watch budget live where the key does: the environment, or the .env file."""

    def setUp(self):
        self.home = Path(install(self))

    def test_a_setting_is_read_from_the_environment_first_and_then_the_file(self):
        (self.home / ".env").write_text("FLIGHTSLEUTH_NTFY_TOPIC=from-file\n", encoding="utf-8")
        self.assertEqual(config.ntfy_topic(), "from-file")
        with mock.patch.dict(os.environ, {config.TOPIC_VAR: "from-env"}):
            self.assertEqual(config.ntfy_topic(), "from-env")

    def test_saving_one_setting_leaves_the_key_and_every_other_line_alone(self):
        (self.home / ".env").write_text("# my notes\nSERPAPI_API_KEY=k\n", encoding="utf-8")
        config.save_setting(config.TOPIC_VAR, "abc-123")
        config.save_setting(config.TOPIC_VAR, "def-456")
        self.assertEqual(
            (self.home / ".env").read_text(encoding="utf-8"),
            "# my notes\nSERPAPI_API_KEY=k\nFLIGHTSLEUTH_NTFY_TOPIC=def-456\n",
        )

    def test_there_is_no_topic_until_one_is_set_up(self):
        self.assertIsNone(config.ntfy_topic())

    def test_a_topic_ntfy_would_refuse_is_refused_without_repeating_it(self):
        with mock.patch.dict(os.environ, {config.TOPIC_VAR: "my secret/topic"}):
            with self.assertRaises(UserError) as caught:
                config.ntfy_topic()
        self.assertNotIn("secret", str(caught.exception))

    def test_alerts_go_to_ntfy_sh_unless_another_server_is_named(self):
        self.assertEqual(config.ntfy_server(), "https://ntfy.sh")
        with mock.patch.dict(os.environ, {config.NTFY_VAR: "https://ntfy.example.org/"}):
            self.assertEqual(config.ntfy_server(), "https://ntfy.example.org")
        with mock.patch.dict(os.environ, {config.NTFY_VAR: "ntfy.example.org"}):
            with self.assertRaises(UserError):
                config.ntfy_server()

    def test_watches_may_use_150_searches_a_month_until_the_owner_raises_it(self):
        self.assertEqual(config.watch_monthly(), 150)
        config.save_setting(config.WATCH_MONTHLY_VAR, "400")
        self.assertEqual(config.watch_monthly(), 400)
        with mock.patch.dict(os.environ, {config.WATCH_MONTHLY_VAR: "lots"}):
            with self.assertRaises(UserError):
                config.watch_monthly()

    def test_the_watch_log_lives_with_the_history(self):
        self.assertEqual(config.log_path(), config.history_path().parent / "watch.log")


if __name__ == "__main__":
    unittest.main()
