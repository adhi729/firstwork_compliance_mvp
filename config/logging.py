import json
import logging
from datetime import UTC, datetime

from apps.tenants.middleware import current_request_id


class RequestJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "request_id": current_request_id.get(),
            },
            ensure_ascii=True,
        )
