from sentry.at_glitchtip_af9a700a8706.utils.safe import get_path, trim

from .v1 import get_values


def get_inner_exception_types(data):
    """Types of the exceptions in the chain other than the main (last) one, nearest to the main exception first."""
    values = get_values(get_path(data, "exception")) or []
    return [trim(get_path(exception, "type", default="Error"), 128) for exception in reversed(values[:-1]) if exception]
