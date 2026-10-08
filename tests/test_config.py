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


if __name__ == "__main__":
    unittest.main()
