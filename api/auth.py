"""Telegram Mini App authentication.

Telegram passes `initData` (a URL-encoded query string) to the WebApp.
We verify its hash using the bot token to ensure the request actually
comes from a Telegram client and identify the user."""

import hmac
import hashlib
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)


def verify_init_data(init_data: str, bot_token: str, max_age_sec: int = 86400) -> Optional[dict]:
    """Verify Telegram WebApp initData signature.

    Returns user dict ({id, first_name, ...}) on success, None on failure.
    """
    if not init_data or not bot_token:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        recv_hash = parsed.pop("hash", None)
        if not recv_hash:
            return None

        # Build data-check-string
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calc_hash, recv_hash):
            logger.warning("initData hash mismatch")
            return None

        # Check freshness
        auth_date = int(parsed.get("auth_date", 0))
        if auth_date and (time.time() - auth_date) > max_age_sec:
            logger.warning("initData too old (%ds)", time.time() - auth_date)
            return None

        user_json = parsed.get("user")
        if user_json:
            try:
                return json.loads(user_json)
            except Exception:
                pass
        return {}
    except Exception:
        logger.exception("verify_init_data failed")
        return None
