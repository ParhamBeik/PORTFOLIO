import logging


logger = logging.getLogger(__name__)


def init_sentry(dsn: str, *, environment="production") -> bool:
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.error("SENTRY_DSN is set but sentry-sdk is not installed.")
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        send_default_pii=False,
        traces_sample_rate=0,
    )
    return True
