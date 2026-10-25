"""Bounded operational logs with credential and identifier redaction."""

from __future__ import annotations

import datetime
import logging
import re
from collections import deque


def redact(value: str) -> str:
    value = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", value, flags=re.I)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[JWT]", value)
    value = re.sub(
        r'(?i)((?:password|bot_token|jwt_token|context_token|token|secret)["\']?\s*[:=]\s*)'
        r'(?:"[^"]*"|\'[^\']*\'|[^\s,}]+)',
        r"\1[redacted]", value,
    )
    value = re.sub(r"(/login\s+)\S+\s+\S+", r"\1[redacted]", value, flags=re.I)
    return value


class LogBuffer(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.entries: deque[dict] = deque(maxlen=300)
        self.sequence = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.sequence += 1
        self.entries.append({
            "id": self.sequence,
            "at": datetime.datetime.fromtimestamp(
                record.created, datetime.UTC
            ).isoformat(),
            "level": record.levelname,
            "source": record.name.removeprefix("open_webui_weixin."),
            "message": redact(record.getMessage())[:1600],
        })
