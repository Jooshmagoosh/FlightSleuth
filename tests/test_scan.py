import unittest
from datetime import date

from flightsleuth import scan
from tests.fakes import FakeSerpApi, install


def request(**changes):
    return scan.Request("JFK", "LHR", date(2030, 3, 12), **changes)


class CapTest(unittest.TestCase):
    def setUp(self):
        install(self)

    def test_run_refuses_a_plan_over_the_cap_whoever_calls_it(self):
        plan = scan.plan(request(flex=13))  # 27 departure dates
        with self.assertRaises(scan.OverCap) as caught:
            scan.run(plan)
        self.assertEqual((caught.exception.live, caught.exception.limit), (27, 25))
        self.assertEqual(FakeSerpApi.searches, [])

    def test_the_cap_can_be_raised_on_purpose(self):
        scan.run(scan.plan(request(flex=13)), max_searches=27)
        self.assertEqual(len(FakeSerpApi.searches), 27)

    def test_saved_replies_do_not_count_towards_the_cap(self):
        scan.run(scan.plan(request(flex=12)))  # 25 live: allowed
        plan = scan.plan(request(flex=13))
        self.assertEqual((plan.saved, plan.live), (25, 2))
        scan.run(plan)
        self.assertEqual(len(FakeSerpApi.searches), 27)


if __name__ == "__main__":
    unittest.main()
