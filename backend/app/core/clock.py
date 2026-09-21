"""What day it is - for the users, not for the server.

A container runs in UTC. A user in Lyon asking at 00:30 on 1 October is on
the day an amendment takes effect; the server, still on 30 September, is not.
The date that reaches the model is the calendar day of the deployment's
timezone (KP_TIMEZONE), the one its users live in.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings


def today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()
