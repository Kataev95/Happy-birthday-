"""Birthday validation and leap-day rules."""
from datetime import date, timedelta


def validate_birthday(day: int, month: int, year: int | None) -> None:
    if isinstance(day, bool) or isinstance(month, bool):
        raise ValueError("Incorrect date")
    if year is not None:
        if isinstance(year, bool) or not (1900 <= year <= date.today().year):
            raise ValueError("Incorrect year")
        date(year, month, day)
        if date(year, month, day) > date.today():
            raise ValueError("Birthdate cannot be in the future")
    else:
        date(2000, month, day)  # leap year: Feb 29 is valid without birth year


def day_of_observance(day: int, month: int, year: int) -> date:
    """Celebrate Feb 29 on Feb 28 in non-leap years."""
    if day == 29 and month == 2:
        try:
            return date(year, 2, 29)
        except ValueError:
            return date(year, 2, 28)
    return date(year, month, day)


def next_occurrence(day: int, month: int, today: date) -> date:
    result = day_of_observance(day, month, today.year)
    if result < today:
        result = day_of_observance(day, month, today.year + 1)
    return result


def days_until(day: int, month: int, today: date) -> int:
    return (next_occurrence(day, month, today) - today).days
