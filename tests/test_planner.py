import unittest
from datetime import date

from flightsleuth.models import Query
from flightsleuth.planner import date_pairs, one_way_legs

TODAY = date(2030, 3, 1)


class DatePairsTest(unittest.TestCase):
    def test_fixed_dates_give_one_pair(self):
        pairs = date_pairs(date(2030, 3, 12), return_date=date(2030, 3, 19), today=TODAY)
        self.assertEqual(pairs, [(date(2030, 3, 12), date(2030, 3, 19))])

    def test_flex_slides_the_trip_and_keeps_its_length(self):
        pairs = date_pairs(date(2030, 3, 12), return_date=date(2030, 3, 19), flex=1, today=TODAY)
        self.assertEqual(
            pairs,
            [
                (date(2030, 3, 11), date(2030, 3, 18)),
                (date(2030, 3, 12), date(2030, 3, 19)),
                (date(2030, 3, 13), date(2030, 3, 20)),
            ],
        )

    def test_flex_never_reaches_into_the_past(self):
        pairs = date_pairs(date(2030, 3, 2), flex=3, today=TODAY)
        self.assertEqual([depart for depart, _ in pairs][0], TODAY)
        self.assertEqual(len(pairs), 5)

    def test_nights_range_tries_every_length_for_every_departure(self):
        pairs = date_pairs(date(2030, 3, 12), nights=(5, 7), flex=1, today=TODAY)
        self.assertEqual(len(pairs), 9)
        self.assertIn((date(2030, 3, 11), date(2030, 3, 16)), pairs)
        self.assertIn((date(2030, 3, 13), date(2030, 3, 20)), pairs)

    def test_one_way_has_no_return(self):
        self.assertEqual(date_pairs(date(2030, 3, 12), today=TODAY), [(date(2030, 3, 12), None)])


class OneWayLegsTest(unittest.TestCase):
    def test_return_leg_flies_the_route_backwards_on_the_return_date(self):
        trip = Query("JFK,EWR", "LHR", date(2030, 3, 12), date(2030, 3, 19), stops="nonstop")
        out, back = one_way_legs(trip)
        self.assertEqual(out, Query("JFK,EWR", "LHR", date(2030, 3, 12), stops="nonstop"))
        self.assertEqual(back, Query("LHR", "JFK,EWR", date(2030, 3, 19), stops="nonstop"))


if __name__ == "__main__":
    unittest.main()
