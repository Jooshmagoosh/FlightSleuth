import unittest
from datetime import date, datetime

from flightsleuth.models import Offer, Query, Segment, itinerary_key

TRIP = Query("JFK,EWR", "LHR", date(2030, 3, 12), date(2030, 3, 19))


def flight(number, origin, destination, leaves, lands, carrier="United"):
    return Segment(carrier, number, origin, destination, datetime.fromisoformat(leaves), datetime.fromisoformat(lands))


OUT = (
    flight("SK 904", "EWR", "ARN", "2030-03-12 17:40", "2030-03-13 07:40", "Scandinavian Airlines"),
    flight("SK 531", "ARN", "LHR", "2030-03-13 11:10", "2030-03-13 12:55", "Scandinavian Airlines"),
)
BACK = (flight("UA 15", "LHR", "EWR", "2030-03-19 15:10", "2030-03-19 18:30"),)


class ItineraryKeyTest(unittest.TestCase):
    def test_key_lists_each_flight_and_when_it_leaves(self):
        self.assertEqual(itinerary_key(OUT), "SK904@2030-03-12T17:40>SK531@2030-03-13T11:10")

    def test_same_flights_match_however_a_source_writes_them(self):
        elsewhere = (
            flight("sk904", "EWR", "ARN", "2030-03-12 17:40", "2030-03-13 07:40", "SAS"),
            flight("SK  531", "ARN", "LHR", "2030-03-13 11:10", "2030-03-13 12:55", "SAS"),
        )
        self.assertEqual(itinerary_key(elsewhere), itinerary_key(OUT))

    def test_another_departure_time_or_flight_is_another_itinerary(self):
        later = (OUT[0], flight("SK 527", "ARN", "LHR", "2030-03-13 15:20", "2030-03-13 17:05"))
        next_day = (flight("SK 904", "EWR", "ARN", "2030-03-13 17:40", "2030-03-14 07:40"), OUT[1])
        self.assertNotEqual(itinerary_key(later), itinerary_key(OUT))
        self.assertNotEqual(itinerary_key(next_day), itinerary_key(OUT))


class OfferTest(unittest.TestCase):
    def test_summary_fields_come_from_the_segments(self):
        offer = Offer(TRIP, 647, OUT, duration_min=855)
        self.assertEqual((offer.origin, offer.destination, offer.stops), ("EWR", "LHR", 1))
        self.assertEqual(offer.depart_at, datetime(2030, 3, 12, 17, 40))
        self.assertEqual(offer.arrive_at, datetime(2030, 3, 13, 12, 55))
        self.assertEqual(offer.airlines, ("Scandinavian Airlines",))

    def test_airlines_are_listed_once_each_in_flying_order(self):
        mixed = (OUT[0], flight("BA 777", "ARN", "LHR", "2030-03-13 11:10", "2030-03-13 12:55", "British Airways"))
        self.assertEqual(Offer(TRIP, 700, mixed, 855).airlines, ("Scandinavian Airlines", "British Airways"))

    def test_nothing_is_claimed_until_a_source_says_it(self):
        offer = Offer(TRIP, 647, OUT, duration_min=855)
        self.assertIsNone(offer.return_segments)
        self.assertIsNone(offer.seller)
        self.assertIsNone(offer.fetched_at)
        self.assertEqual(offer.source, "")
        self.assertFalse(offer.verified_live)
        self.assertFalse(offer.self_transfer or offer.hidden_city or offer.separate_tickets or offer.basic_economy)

    def test_key_covers_the_return_flights_once_they_are_known(self):
        outbound_only = Offer(TRIP, 647, OUT, duration_min=855)
        both_ways = Offer(TRIP, 647, OUT, duration_min=855, return_segments=BACK)
        self.assertEqual(outbound_only.itinerary_key, itinerary_key(OUT))
        self.assertEqual(both_ways.itinerary_key, itinerary_key(OUT) + "|UA15@2030-03-19T15:10")


if __name__ == "__main__":
    unittest.main()
