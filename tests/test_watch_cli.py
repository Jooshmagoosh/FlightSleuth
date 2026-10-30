"""The watch commands, through the real command line. The clock here is the real one, so every date is far ahead."""
import contextlib
import io
import os
import re
import unittest
import urllib.error
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from flightsleuth import cli, config, watch
from flightsleuth.history import History
from tests.fakes import STAMP, FakeNtfy, FakeSerpApi, install, run_cli

TODAY = date.today()
DEPART = TODAY + timedelta(days=400)
BACK = DEPART + timedelta(days=7)
ROUTE = ("JFK", "LHR", "--depart", DEPART.isoformat(), "--return", BACK.isoformat())
TOPIC = "flightsleuth-test-topic"
DAY = r"\w{3} \d\d \w{3}"  # a date, such as "Tue 08 Dec"


class WatchCommandCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(install(self))
        FakeSerpApi.rich = True
        FakeSerpApi.fare = lambda query: 520
        # A plan with a month to run from today, whatever today is.
        FakeSerpApi.account_reply.update(
            total_searches_left=250, searches_per_month=250, plan_renewal_date=(TODAY + timedelta(days=30)).isoformat()
        )
        self.enterContext(mock.patch.dict(os.environ, {config.TOPIC_VAR: TOPIC}))

    def run_watch(self, *argv):
        return run_cli("watch", *argv)

    def ok(self, *argv):
        code, out, err = self.run_watch(*argv)
        self.assertEqual(code, 0, err)
        return out

    def log(self):
        return config.log_path().read_text(encoding="utf-8")


class AddCommandTest(WatchCommandCase):
    def test_adding_shows_the_sums_and_says_it_cost_nothing(self):
        out = self.ok("add", *ROUTE, "--below", "450", "--every", "4")
        lines = out.splitlines()
        self.assertRegex(lines[0], rf"^Until the searches reset on {DAY}:$")
        self.assertRegex(lines[1], rf"^  JFK -> LHR, {DAY} - {DAY}: 8 checks, one every 4 days$")
        self.assertEqual(lines[2], "  8 checks at 1 search each, and 3 held back for each of 1 watch to confirm an alert: 8 + 3 = 11")
        self.assertEqual(lines[3], "  Watches have used 0 of the 150 searches a month they may, which leaves 150, so 139 would be spare.")
        self.assertRegex(lines[-2], rf"^Watching, as number 1: JFK -> LHR, {DAY} - {DAY}\. Adding it used no searches\.$")
        self.assertEqual(lines[-1], "It is checked the next time the watches are run: py -m flightsleuth watch run")
        self.assertEqual((FakeSerpApi.searches, FakeNtfy.posts), ([], []))

    def test_the_rules_and_the_trip_are_kept_as_given(self):
        self.ok("add", "jfk,ewr", "LHR", "--depart", DEPART.isoformat(), "--nights", "5", "--below", "$1,450.50",
                "--drop", "20", "--no-typical", "--every", "3", "--stops", "nonstop", "--cabin", "business", "--adults", "2")  # fmt: skip
        (found,) = [standing.watch for standing in watch.listed()]
        wanted = watch.trip("JFK,EWR", "LHR", DEPART, DEPART + timedelta(days=5), adults=2, cabin="business", stops="nonstop")
        self.assertEqual(found, watch.Watch(1, wanted, 1450.5, 20.0, False, 3, TODAY))

    def test_a_one_way_can_be_watched(self):
        self.ok("add", "JFK", "LHR", "--depart", DEPART.isoformat(), "--one-way", "--below", "200")
        self.assertIsNone(watch.listed()[0].watch.trip.return_date)

    def test_a_watch_the_searches_cannot_cover_is_refused_with_the_sums_shown(self):
        FakeSerpApi.account_reply["total_searches_left"] = 9
        code, out, err = self.run_watch("add", *ROUTE)
        self.assertEqual((code, out), (1, ""))
        lines = err.splitlines()
        self.assertEqual(lines[2], "  30 checks at 1 search each, and 3 held back for each of 1 watch to confirm an alert: 30 + 3 = 33")
        self.assertEqual(lines[3], "  9 searches are left, so that is 24 too many.")
        self.assertEqual(lines[-1], "flightsleuth: the searches cannot cover this watch. Checked every 5 days it would fit.")
        self.assertEqual(watch.listed(), [])

    def test_a_dry_run_shows_the_sums_and_adds_nothing(self):
        out = self.ok("add", *ROUTE, "--dry-run")
        self.assertRegex(out.splitlines()[-1], rf"^That fits\. Nothing was added: JFK -> LHR, {DAY} - {DAY}\.$")
        self.assertEqual(watch.listed(), [])

    def test_a_fourth_watch_is_refused(self):
        for _ in range(3):
            self.ok("add", *ROUTE, "--every", "10")
        code, _, err = self.run_watch("add", *ROUTE, "--every", "10")
        self.assertEqual(code, 1)
        self.assertIn("there are already 3 watches, which is the most", err)

    def test_adding_before_alerts_are_set_up_says_what_to_run(self):
        with mock.patch.dict(os.environ, {config.TOPIC_VAR: ""}):
            out = self.ok("add", *ROUTE)
        self.assertEqual(
            out.splitlines()[-1],
            "Alerts are not set up yet, and nothing is checked until they are. Run: py -m flightsleuth watch setup",
        )

    def test_nights_of_nought_and_a_price_that_is_not_one_are_refused(self):
        code, _, err = self.run_watch("add", "JFK", "LHR", "--depart", DEPART.isoformat(), "--nights", "0")
        self.assertEqual(code, 1)
        self.assertIn("--nights must be 1 or more", err)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            cli.main(["watch", "add", *ROUTE, "--below", "cheap"])
        self.assertIn("'cheap' is not a price like 450", err.getvalue())


class ListCommandTest(WatchCommandCase):
    def test_with_no_watches_it_says_how_to_add_one(self):
        self.assertIn("There are no watches. Add one with: py -m flightsleuth watch add JFK LHR", self.ok("list"))

    def test_a_new_watch_shows_its_rules_and_that_it_is_due(self):
        self.ok("add", *ROUTE, "--below", "450", "--every", "4", "--adults", "2", "--stops", "1")
        lines = self.ok("list").splitlines()
        self.assertRegex(lines[0], rf"^1\. JFK -> LHR, {DAY} - {DAY}, round trip, 2 adults, economy, 1 stop or fewer$")
        self.assertEqual(
            lines[1:5],
            [
                "   Alert when it is below $450, or 15% under its 30-day median, or below Google's typical range. "
                "Checked every 4 days.",
                "   Last check: none yet.",
                "   Last price: none yet.",
                "   Next due: now. Searches used: 0.",
            ],
        )
        self.assertEqual(lines[-1], f"What each run did is in {config.log_path()}")
        self.assertEqual(FakeSerpApi.searches, [])

    def test_after_a_check_it_shows_the_last_check_the_last_price_the_next_due_date_and_the_searches_used(self):
        self.ok("add", *ROUTE, "--below", "600", "--every", "4")
        self.ok("run")
        lines = self.ok("list").splitlines()
        self.assertRegex(lines[2], rf"^   Last check: {STAMP}\. \$520 confirmed\. Alert sent: below the \$600 you set\.$")
        self.assertRegex(lines[3], rf"^   Last price: \$520 confirmed, fetched {STAMP}\.$")
        due = (TODAY + timedelta(days=4)).strftime("%a %d %b")
        self.assertEqual(lines[4], f"   Next due: {due}. Searches used: 3.")

    def test_a_listed_price_nobody_confirmed_is_called_listed(self):
        self.ok("add", *ROUTE)
        self.ok("run")
        self.assertRegex(self.ok("list"), rf"\n   Last price: \$520 listed, fetched {STAMP}\.\n")

    def test_a_failed_check_is_the_status_line_and_the_watch_stays_due(self):
        self.ok("add", *ROUTE)
        FakeSerpApi.fail_after = 0
        self.run_watch("run")
        lines = self.ok("list").splitlines()
        self.assertRegex(lines[2], rf"^   Last check: {STAMP}\. Check failed: stopped after 0 of 1 live searches: .* It stays due\.$")
        self.assertEqual(lines[3:5], ["   Last price: none yet.", "   Next due: now. Searches used: 0."])

    def test_an_alert_that_did_not_go_is_shown_until_it_does(self):
        self.ok("add", *ROUTE, "--below", "600", "--every", "4")
        FakeNtfy.fail = urllib.error.URLError("no route to host")
        self.ok("run")
        self.assertIn("\n   An alert about $520 has not reached ntfy. The next run tries again.\n", self.ok("list"))
        FakeNtfy.fail = None
        self.assertIn("the alert about $520 that did not go before has now been sent.", self.ok("run"))
        self.assertNotIn("has not reached ntfy", self.ok("list"))

    def test_removed_watches_are_left_out_unless_asked_for(self):
        self.ok("add", *ROUTE)
        self.ok("add", "BOS", "WAW", "--depart", DEPART.isoformat(), "--one-way")
        self.ok("remove", "1")
        out = self.ok("list")
        self.assertNotIn("JFK -> LHR", out)
        self.assertRegex(out, rf"^2\. BOS -> WAW, {DAY}, one way, 1 adult, economy\n")
        self.assertIn("\n   Removed. Searches used: 0.\n", self.ok("list", "--all"))


class RemoveCommandTest(WatchCommandCase):
    def test_removing_names_the_watch_and_says_its_history_is_kept(self):
        self.ok("add", *ROUTE)
        self.assertRegex(self.ok("remove", "1"), rf"^Removed watch 1: JFK -> LHR, {DAY} - {DAY}\. Its history is kept\.\n$")

    def test_a_number_that_is_not_a_watch_is_an_error(self):
        code, _, err = self.run_watch("remove", "4")
        self.assertEqual((code, err), (1, "flightsleuth: there is no watch 4. See them with: py -m flightsleuth watch list\n"))


class RunCommandTest(WatchCommandCase):
    """The command the scheduler starts: check what is due, once, say what happened, write it to the log, and exit."""

    def test_it_checks_what_is_due_prints_a_line_for_each_and_exits_cleanly(self):
        self.ok("add", *ROUTE)
        code, out, err = self.run_watch("run")
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, rf"^JFK -> LHR, {DAY} - {DAY}: \$520 listed\. No rule fired\. 1 search\.\n$")
        self.assertEqual(len(FakeSerpApi.searches), 1)

    def test_the_same_line_goes_into_the_log_with_the_time_of_the_run(self):
        self.ok("add", *ROUTE)
        out = self.ok("run")
        self.assertRegex(self.log(), r"^\d{4}-\d\d-\d\d \d\d:\d\d  " + re.escape(out) + "$")

    def test_it_never_asks_a_question(self):
        self.ok("add", *ROUTE, "--below", "600")
        with mock.patch("builtins.input", side_effect=AssertionError("an unattended run cannot answer")):
            self.ok("run")

    def test_a_second_run_the_same_day_spends_nothing(self):
        self.ok("add", *ROUTE)
        self.ok("run")
        self.assertRegex(self.ok("run"), rf": not due until {DAY}\.\n$")
        self.assertEqual(len(FakeSerpApi.searches), 1)

    def test_a_failed_check_exits_with_an_error_code_and_is_logged(self):
        self.ok("add", *ROUTE)
        FakeSerpApi.fail_after = 0
        code, out, _ = self.run_watch("run")
        self.assertEqual(code, 1)
        self.assertIn("Check failed: stopped after 0 of 1 live searches", out)
        self.assertIn("Check failed: stopped after 0 of 1 live searches", self.log())

    def test_without_a_topic_nothing_is_spent_and_the_log_says_why(self):
        self.ok("add", *ROUTE)
        with mock.patch.dict(os.environ, {config.TOPIC_VAR: ""}):
            code, _, err = self.run_watch("run")
        self.assertEqual(code, 1)
        self.assertEqual(err, "flightsleuth: no alert topic is set up, so nothing was checked. Run: py -m flightsleuth watch setup\n")
        self.assertIn("  Nothing was checked: no alert topic is set up", self.log())
        self.assertEqual(FakeSerpApi.searches, [])

    def test_something_unexpected_is_written_to_the_log_before_it_is_raised(self):
        self.ok("add", *ROUTE)
        with mock.patch.object(watch, "run_due", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_watch("run")
        self.assertIn("  The run stopped unexpectedly: RuntimeError('boom')\n", self.log())

    def test_a_dry_run_spends_nothing_and_writes_no_log(self):
        self.ok("add", *ROUTE)
        out = self.ok("run", "--dry-run")
        self.assertRegex(out, r": due\. 1 search, and up to 3 more if the price is worth an alert\.\n$")
        self.assertEqual(FakeSerpApi.searches, [])
        self.assertFalse(config.log_path().exists())

    def test_a_limit_on_the_run_is_kept(self):
        self.ok("add", *ROUTE, "--below", "600")
        out = self.ok("run", "--max-searches", "2")
        self.assertEqual(len(FakeSerpApi.searches), 2)
        self.assertIn("Alert sent", out)
        self.assertIn("Who sells it was not looked up.", FakeNtfy.sent[0]["message"])

    def test_the_topic_never_appears_in_what_is_printed_or_logged(self):
        self.ok("add", *ROUTE, "--below", "600")
        out = self.ok("run") + self.ok("list") + self.ok("test-alert")
        self.assertNotIn(TOPIC, out + self.log())


class SetupCommandTest(WatchCommandCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {config.TOPIC_VAR: ""}))

    def test_it_makes_a_topic_keeps_it_out_of_git_and_says_how_to_subscribe(self):
        out = self.ok("setup")
        topic = config.ntfy_topic()
        self.assertRegex(topic, r"^flightsleuth-[-_A-Za-z0-9]{43}$")
        self.assertEqual(
            out.splitlines(),
            [
                f"A new alert topic was made and saved in {self.home / '.env'}",
                "",
                f"  {topic}",
                "",
                "Keep it to yourself: anyone who knows the topic can read your alerts. It is in no file that git tracks.",
                'On your phone, install the ntfy app, choose "Subscribe to topic" and enter the topic above. '
                "The server is https://ntfy.sh",
                "Then send yourself a test, which makes no search: py -m flightsleuth watch test-alert",
            ],
        )
        self.assertEqual((FakeNtfy.posts, FakeSerpApi.searches), ([], []))

    def test_running_it_again_shows_the_same_topic(self):
        self.ok("setup")
        topic = config.ntfy_topic()
        out = self.ok("setup")
        self.assertIn("The alert topic is already set up. To replace it: py -m flightsleuth watch setup --new", out)
        self.assertIn(f"\n  {topic}\n", out)

    def test_a_new_topic_can_be_asked_for(self):
        self.ok("setup")
        old = config.ntfy_topic()
        out = self.ok("setup", "--new")
        self.assertNotEqual(config.ntfy_topic(), old)
        self.assertIn("Remove the old topic from the app: nothing is sent to it any more.", out)

    def test_the_projects_gitignore_keeps_the_env_file_out_of_the_repository(self):
        ignored = (Path(cli.__file__).parent.parent / ".gitignore").read_text(encoding="utf-8").split()
        self.assertIn(".env", ignored)


class TestAlertCommandTest(WatchCommandCase):
    def test_it_sends_one_message_and_makes_no_search(self):
        out = self.ok("test-alert")
        self.assertEqual(
            out.splitlines(),
            [
                "Test alert sent. It should reach your phone within a few seconds. No search was made.",
                "It opens nothing when tapped, because there is no watch yet.",
            ],
        )
        self.assertEqual(([sent["title"] for sent in FakeNtfy.sent], FakeSerpApi.searches), (["FlightSleuth test alert"], []))
        history = History(config.history_path())
        self.addCleanup(history.close)
        self.assertEqual(history.rows("provider_calls"), [])

    def test_with_a_watch_it_says_what_tapping_it_opens(self):
        self.ok("add", *ROUTE)
        out = self.ok("test-alert")
        self.assertIn(f"Tapping it opens https://www.momondo.com/flight-search/JFK-LHR/{DEPART}/{BACK}?sort=price_a", out)

    def test_before_setup_it_says_what_to_run(self):
        with mock.patch.dict(os.environ, {config.TOPIC_VAR: ""}):
            code, _, err = self.run_watch("test-alert")
        self.assertEqual((code, err), (1, "flightsleuth: no alert topic is set up yet. Run: py -m flightsleuth watch setup\n"))

    def test_a_message_ntfy_would_not_take_is_an_error(self):
        FakeNtfy.fail = urllib.error.URLError("no route to host")
        code, out, err = self.run_watch("test-alert")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("the alert could not be sent: network error: no route to host", err)


class LimitCommandTest(WatchCommandCase):
    def test_it_shows_the_ceiling(self):
        self.assertEqual(self.ok("limit"), "Watches may use up to 150 searches a month, and never more than the plan has left.\n")

    def test_the_owner_can_raise_it_and_it_is_kept(self):
        out = self.ok("limit", "400")
        self.assertEqual(
            out.splitlines(),
            [f"Saved in {self.home / '.env'}", "Watches may use up to 400 searches a month, and never more than the plan has left."],
        )
        self.assertEqual(config.watch_monthly(), 400)
        self.assertEqual((self.home / ".env").read_text(encoding="utf-8"), "FLIGHTSLEUTH_WATCH_MONTHLY=400\n")

    def test_the_new_ceiling_decides_what_can_be_added(self):
        self.ok("limit", "20")
        self.assertEqual(self.run_watch("add", *ROUTE)[0], 1)
        self.ok("limit", "400")
        self.assertEqual(self.run_watch("add", *ROUTE)[0], 0)


class GateTest(unittest.TestCase):
    """What Phase 4 promised that nothing else pins: the limits, and that the documents say how to run it."""

    ROOT = Path(cli.__file__).parent.parent

    def test_at_most_three_watches_and_three_searches_held_for_each(self):
        self.assertEqual((watch.MAX_WATCHES, watch.HOLD), (3, 3))

    def test_a_further_drop_is_five_dollars_and_two_percent_and_the_median_is_of_thirty_days_at_fifteen_percent(self):
        self.assertEqual((watch.FURTHER_DROP, watch.FURTHER_PERCENT), (5, 2))
        self.assertEqual((watch.MEDIAN_DAYS, watch.DEFAULT_DROP), (30, 15))

    def test_the_ceiling_is_150_a_month_unless_the_owner_changes_it(self):
        self.assertEqual(config.WATCH_MONTHLY, 150)

    def test_the_readme_gives_the_command_that_schedules_the_run_and_makes_up_a_missed_one(self):
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        (command,) = [line for line in readme.splitlines() if line.startswith("$action = New-ScheduledTaskAction")]
        self.assertIn('-Argument "-m flightsleuth watch run"', command)
        self.assertIn("(Get-Command pyw).Source", command)  # no console window, and no path written down
        self.assertIn("New-ScheduledTaskSettingsSet -StartWhenAvailable", readme)
        self.assertIn('Register-ScheduledTask -TaskName "FlightSleuth watches"', readme)

    def test_nothing_in_the_repository_schedules_it_for_the_owner(self):
        for path in (self.ROOT / "flightsleuth").rglob("*.py"):
            self.assertNotIn("ScheduledTask", path.read_text(encoding="utf-8"), path.name)

    def test_the_beginners_guide_describes_the_button(self):
        guide = (self.ROOT / "HOW-TO-USE.txt").read_text(encoding="utf-8")
        self.assertIn('"Watch this fare..."', guide)
        self.assertIn("You can\nhave 3 watches at most.", guide)

    def test_no_topic_is_written_in_any_tracked_document(self):
        for name in ("README.md", "HOW-TO-USE.txt", "CLAUDE.md", ".env.example", "docs/ROADMAP.md"):
            text = (self.ROOT / name).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"flightsleuth-[-_A-Za-z0-9]{40,}", text), name)


if __name__ == "__main__":
    unittest.main()
