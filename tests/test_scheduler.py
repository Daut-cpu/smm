from datetime import datetime, time
from zoneinfo import ZoneInfo

from kino_bot.scheduler import next_run

TZ = ZoneInfo("Europe/Moscow")


def test_next_run_today_and_tomorrow():
    now = datetime(2026, 11, 18, 9, 0, tzinfo=TZ)
    assert next_run(now, time(10, 0)) == datetime(2026, 11, 18, 10, 0, tzinfo=TZ)
    now = datetime(2026, 11, 18, 10, 0, tzinfo=TZ)
    assert next_run(now, time(10, 0)) == datetime(2026, 11, 19, 10, 0, tzinfo=TZ)
