import io
import os
import re
import unittest
import urllib.error
from unittest import mock

from flightsleuth import config, notify
from tests.fakes import FakeNtfy, install

TOPIC = "flightsleuth-test-topic"
ALERT = notify.Alert(
    title="JFK to LHR: $455 round trip",
    message="Mon 11 Mar - Mon 18 Mar. Below the $500 you set.",
    click="https://www.momondo.com/flight-search/JFK-LHR/2030-03-11/2030-03-18?sort=price_a",
)


class SendTest(unittest.TestCase):
    """An alert is one POST of JSON to ntfy, as its documentation described on 2026-10-09."""

    def setUp(self):
        install(self)
        self.enterContext(mock.patch.dict(os.environ, {config.TOPIC_VAR: TOPIC}))

    def test_an_alert_carries_its_title_its_text_and_the_address_to_open(self):
        notify.send(ALERT)
        self.assertEqual(
            FakeNtfy.sent, [{"topic": TOPIC, "title": ALERT.title, "message": ALERT.message, "click": ALERT.click}]
        )

    def test_it_is_posted_to_the_servers_root_so_the_topic_is_never_in_an_address(self):
        notify.send(ALERT)
        ((address, headers),) = FakeNtfy.posts
        self.assertEqual(address, "https://ntfy.sh")
        self.assertEqual(headers["Content-type"], "application/json")
        self.assertNotIn(TOPIC, address + repr(headers))

    def test_an_alert_with_nothing_to_open_sends_no_address(self):
        notify.send(notify.Alert("Test", "Nothing to open."))
        self.assertNotIn("click", FakeNtfy.sent[0])

    def test_text_outside_ascii_survives(self):
        notify.send(notify.Alert("Zürich", "€455 – a fare"))
        self.assertEqual((FakeNtfy.sent[0]["title"], FakeNtfy.sent[0]["message"]), ("Zürich", "€455 – a fare"))

    def test_another_server_can_be_named(self):
        with mock.patch.dict(os.environ, {config.NTFY_VAR: "https://ntfy.example.org/"}):
            notify.send(ALERT)
        self.assertEqual(FakeNtfy.posts[0][0], "https://ntfy.example.org")

    def test_without_a_topic_nothing_is_sent_and_the_way_to_get_one_is_given(self):
        with mock.patch.dict(os.environ, {config.TOPIC_VAR: ""}):
            with self.assertRaisesRegex(notify.NotifyError, "py -m flightsleuth watch setup"):
                notify.send(ALERT)
        self.assertEqual(FakeNtfy.posts, [])

    def test_a_refusal_is_reported_in_ntfys_words_without_the_topic(self):
        body = io.BytesIO(b'{"code": 42901, "http": 429, "error": "limit reached for %s"}' % TOPIC.encode())
        FakeNtfy.fail = urllib.error.HTTPError("https://ntfy.sh", 429, "Too Many Requests", {}, body)
        with self.assertRaises(notify.NotifyError) as caught:
            notify.send(ALERT)
        self.assertEqual(str(caught.exception), "ntfy refused the alert: HTTP 429, limit reached for <topic>")

    def test_a_network_failure_is_reported_and_not_retried(self):
        FakeNtfy.fail = urllib.error.URLError("no route to host")
        with self.assertRaisesRegex(notify.NotifyError, "could not be sent: network error: no route to host"):
            notify.send(ALERT)
        # Once only: a message that did arrive must never be followed by a second copy.
        self.assertEqual(len(FakeNtfy.posts), 1)


class TopicTest(unittest.TestCase):
    def test_a_new_topic_is_long_random_and_made_of_what_ntfy_allows(self):
        first, second = notify.new_topic(), notify.new_topic()
        self.assertNotEqual(first, second)
        for topic in (first, second):
            self.assertTrue(re.fullmatch(r"flightsleuth-[-_A-Za-z0-9]{43}", topic), topic)
            self.assertLessEqual(len(topic), 64)

    def test_a_new_topic_is_one_the_settings_accept(self):
        install(self)
        config.save_setting(config.TOPIC_VAR, notify.new_topic())
        self.assertTrue(config.ntfy_topic().startswith("flightsleuth-"))


if __name__ == "__main__":
    unittest.main()
