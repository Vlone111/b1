"""Common utilities for cabinet schemas."""

from datetime import datetime

from pydantic import field_serializer

from app.utils.timezone import format_local_datetime


def create_datetime_serializer():
    """Create a field serializer for datetime fields that converts to local timezone."""

    @field_serializer('created_at', 'updated_at', 'start_date', 'end_date', 'expires_at', 'closed_at',
                      'processed_at', 'first_seen', 'last_seen', 'timestamp', 'punished_at', 'enable_at',
                      'enabled_at', 'last_report', 'detected_at', 'last_activity', 'subscription_end_date',
                      'cabinet_last_login', 'promo_offer_discount_expires_at', 'first_connected_at',
                      'online_at', 'expire_at', 'last_sync', 'bot_subscription_end_date', 'panel_expire_at',
                      'paid_at', 'delivered_at', 'active_discount_expires_at', 'completed_at',
                      'last_updated', 'last_status_change', 'next_run', 'last_run_started_at',
                      'last_run_finished_at', 'next_daily_charge_at')
    def serialize_datetime(self, value: datetime) -> str | None:
        """Serialize datetime to local timezone ISO format string."""
        if value is None:
            return None
        return format_local_datetime(value, '%Y-%m-%dT%H:%M:%S')

    return serialize_datetime
