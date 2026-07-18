"""Layer 3 — cumulative usage cap (see docs/design-notes.md §3.4)."""

import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

USAGE_TABLE_NAME = os.environ["USAGE_TABLE_NAME"]
DAILY_LIMIT = int(os.environ["DAILY_LIMIT"])
MONTHLY_LIMIT = int(os.environ["MONTHLY_LIMIT"])

dynamodb = boto3.resource("dynamodb")
usage_table = dynamodb.Table(USAGE_TABLE_NAME)


def _increment_and_check(counter_id: str, limit: int) -> bool:
    # Returns True if the counter is still within limit after incrementing.
    response = usage_table.update_item(
        Key={"counter_id": counter_id},
        UpdateExpression="ADD #c :one",
        ExpressionAttributeNames={"#c": "count"},
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    count = int(response["Attributes"]["count"])
    within_limit = count <= limit
    logger.info("Usage counter incremented | counter_id=%s count=%s limit=%s within_limit=%s", counter_id, count, limit, within_limit)
    return within_limit


def usage_cap_reached() -> bool:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    month = time.strftime("%Y-%m", time.gmtime())
    within_daily = _increment_and_check(f"date#{today}", DAILY_LIMIT)
    # Skip the monthly write once the daily cap is already exceeded — the
    # call is rejected either way.
    within_monthly = within_daily and _increment_and_check(f"month#{month}", MONTHLY_LIMIT)
    if not within_daily or not within_monthly:
        # Metric filter target for a CloudWatch alarm — see docs/design-notes.md §3.4 Layer 3.
        logger.error("usage cap reached | daily_ok=%s monthly_ok=%s", within_daily, within_monthly)
        return True
    return False
