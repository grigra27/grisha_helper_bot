"""Conservative deadline parsing anchored to the request's publication date."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
    "ма": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3,
    "пятниц": 4, "суббот": 5, "воскресень": 6,
}
DEADLINE_MARKER = re.compile(r"дедлайн|deadline|срок\s+(?:ответ|коммент)|(?:ответ\w*|комментар\w*)\s+до", re.I)


@dataclass(frozen=True)
class Deadline:
    status: str  # known, absent, ambiguous
    at: datetime | None = None


def parse_deadline(raw: str, published_at: datetime) -> Deadline:
    text = " ".join(raw.casefold().split())
    if not text:
        return Deadline("absent")
    if published_at.tzinfo is None:
        return Deadline("ambiguous")
    if re.fullmatch(r"(?:срочно|оперативно|как можно скорее|asap)[.! ]*", text):
        return Deadline("absent")
    published = published_at.astimezone(MOSCOW).date()
    dates, relatives, weekdays = set(), set(), set()
    month_pattern = r"январ[яь]|феврал[яь]|март[а]?|апрел[яь]|ма[яй]|июн[яь]|июл[яь]|август[а]?|сентябр[яь]|октябр[яь]|ноябр[яь]|декабр[яь]"
    month_dates = list(re.finditer(r"(?<!\d)(\d{1,2})\s+(" + month_pattern + r")(?:\s+(20\d{2}))?", text))
    numeric_dates = list(re.finditer(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?!\d)", text))
    for word, offset in (("сегодня", 0), ("завтра", 1), ("послезавтра", 2)):
        if re.search(r"\b" + word + r"\b", text):
            relatives.add(published + timedelta(days=offset))
    for stem, number in WEEKDAYS.items():
        if re.search(r"\b" + stem + r"\w*\b", text):
            weekdays.add(number)
    if len(relatives) > 1 or len(weekdays) > 1:
        return Deadline("ambiguous")

    def add_date(day, month, year):
        if year:
            resolved = int(year)
            dates.add(date(resolved + 2000 if resolved < 100 else resolved, month, day))
        else:
            resolved = published.year
            if published.month == 12 and month == 1:
                resolved += 1
            elif published.month == 1 and month == 12:
                resolved -= 1
            dates.add(date(resolved, month, day))

    def numeric_is_clock(match):
        if match[3]:
            return False
        if not 1 <= int(match[2]) <= 12:
            return True
        suffix = text[match.end():match.end() + 12]
        return bool(re.match(r"\s*(?:час|ч\b)", suffix))

    try:
        for match in month_dates:
            month = next(number for stem, number in MONTHS.items() if match[2].startswith(stem))
            add_date(int(match[1]), month, match[3])
        for match in numeric_dates:
            day, month = int(match[1]), int(match[2])
            # 11.00 is an unambiguous clock; a standalone 23.10 is a calendar
            # date. Conflicting relative dates are never rescued as clocks.
            if numeric_is_clock(match):
                continue
            add_date(day, month, match[3])
    except (StopIteration, ValueError, OverflowError):
        return Deadline("ambiguous")
    if len(dates) > 1:
        return Deadline("ambiguous")
    target = next(iter(dates), None)
    if relatives:
        relative = next(iter(relatives))
        if target and target != relative:
            return Deadline("ambiguous")
        target = relative
    if weekdays:
        weekday = next(iter(weekdays))
        if target and target.weekday() != weekday:
            return Deadline("ambiguous")
        if target is None:
            target = published + timedelta(days=(weekday - published.weekday()) % 7)

    clocks = set()
    for match in re.finditer(r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)", text):
        if int(match[1]) > 23:
            return Deadline("ambiguous")
        clocks.add((int(match[1]), int(match[2])))
    for match in re.finditer(r"(?<!\d)([0-2]?\d)\.([0-5]\d)(?![.\d])", text):
        is_date = any(start.start() <= match.start() < start.end() for start in numeric_dates if not numeric_is_clock(start))
        if not is_date and int(match[1]) <= 23:
            clocks.add((int(match[1]), int(match[2])))
    if not clocks:
        match = re.search(r"(?:до|к|в)\s+([01]?\d|2[0-3])\s*(?:час\w*|ч\b|$)", text)
        if match:
            clocks.add((int(match[1]), 0))
    if len(clocks) > 1:
        return Deadline("ambiguous")
    if target is None and not clocks:
        return Deadline("ambiguous")
    target = target or published
    clock = time(*next(iter(clocks))) if clocks else time(23, 59, 59)
    # Explicit timezones outside the supported local form require review.
    if re.search(r"\butc\b|gmt|по\s+(?:киев|астан|алмат|лондон)|[+-]\d{2}:?\d{2}", text):
        return Deadline("ambiguous")
    return Deadline("known", datetime.combine(target, clock, tzinfo=MOSCOW))
