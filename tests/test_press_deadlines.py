import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from press_deadlines import parse_deadline

MSK = ZoneInfo("Europe/Moscow")


class DeadlineTests(unittest.TestCase):
    def test_explicit_old_date_stays_in_the_past(self):
        published = datetime(2026, 11, 9, 12, tzinfo=MSK)
        parsed = parse_deadline("Дедлайн: 23 октября до позднего вечера", published)
        self.assertEqual(parsed.status, "known")
        self.assertEqual(parsed.at, datetime(2026, 10, 23, 23, 59, 59, tzinfo=MSK))

    def test_relative_day_is_anchored_to_publication_not_processing(self):
        published = datetime(2026, 11, 6, 12, tzinfo=MSK)
        self.assertEqual(parse_deadline("завтра до 18:00", published).at, datetime(2026, 11, 7, 18, tzinfo=MSK))
        self.assertEqual(parse_deadline("сегодня до 18:00", published).at, datetime(2026, 11, 6, 18, tzinfo=MSK))

    def test_conflicting_tomorrow_and_calendar_date_is_rejected(self):
        self.assertEqual(parse_deadline("на завтра 6.11", datetime(2026, 11, 9, 12, tzinfo=MSK)).status, "ambiguous")
        self.assertEqual(parse_deadline("завтра до 6.11", datetime(2026, 11, 9, 12, tzinfo=MSK)).status, "ambiguous")
        self.assertEqual(parse_deadline("понедельник до 6.11", datetime(2026, 11, 6, 12, tzinfo=MSK)).status, "ambiguous")

    def test_weekday_dot_clock_and_same_day_expiry(self):
        self.assertEqual(parse_deadline("понедельник, 11.00", datetime(2026, 11, 6, 12, tzinfo=MSK)).at,
                         datetime(2026, 11, 9, 11, tzinfo=MSK))
        self.assertEqual(parse_deadline("понедельник, 11.00", datetime(2026, 11, 9, 19, tzinfo=MSK)).at,
                         datetime(2026, 11, 9, 11, tzinfo=MSK))

    def test_numeric_date_and_clock_are_both_preserved(self):
        published = datetime(2026, 11, 9, 12, tzinfo=MSK)
        self.assertEqual(parse_deadline("до 10.11.2026, 18:30 МСК", published).at,
                         datetime(2026, 11, 10, 18, 30, tzinfo=MSK))

    def test_year_boundary_does_not_resurrect_old_dates(self):
        self.assertEqual(parse_deadline("2 января", datetime(2026, 12, 30, 12, tzinfo=MSK)).at.year, 2027)
        self.assertEqual(parse_deadline("31 декабря", datetime(2027, 1, 2, 12, tzinfo=MSK)).at.year, 2026)
        parsed = parse_deadline("до 11.05", datetime(2026, 11, 10, 12, tzinfo=MSK))
        self.assertEqual(parsed.at, datetime(2026, 5, 11, 23, 59, 59, tzinfo=MSK))

    def test_absence_urgency_and_ambiguous_inputs(self):
        published = datetime(2026, 11, 9, 12, tzinfo=MSK)
        for raw in ("", "срочно", "asap"):
            self.assertEqual(parse_deadline(raw, published).status, "absent")
        for raw in ("на следующей неделе", "09.11-10.11", "31 ноября", "25 ноября до 25:00", "10 ноября 18:00 UTC"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_deadline(raw, published).status, "ambiguous")


if __name__ == "__main__":
    unittest.main()
