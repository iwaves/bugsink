from django.utils.encoding import force_str

from sentry.at_597d25951d00.grouping.strategies.message import normalize_message_for_grouping
from sentry.at_glitchtip_af9a700a8706.utils.safe import get_path, trim

from .building_blocks.v1 import get_title_for_exception_type_and_value
from .building_blocks.v2 import get_inner_exception_types
from .building_blocks.v3 import get_code_window, get_grouping_frame, get_root_cause


def default_issue_grouper(data, calculated_type, calculated_value):
    """Bugsnag-style 'frame-code' grouping: the root cause's type and the code it was thrown from, but not its message,
    so that ids in messages do not split issues. Without source context we fall back to v3's key."""
    root_cause = get_root_cause(data)
    frame = get_grouping_frame(root_cause) if root_cause is not None else None
    code_window = get_code_window(frame)
    if code_window is None:
        return value_normalized_issue_grouper(data, calculated_type, calculated_value)

    type_ = trim(get_path(root_cause, "type", default="Error"), 128)
    return " ⋄ ".join([type_, str(frame.get("filename") or ""), code_window])


def value_normalized_issue_grouper(data, calculated_type, calculated_value):
    calculated_value = normalize_message_for_grouping(force_str(calculated_value))
    title = get_title_for_exception_type_and_value(calculated_type, calculated_value)

    inner_types = get_inner_exception_types(data)
    if not inner_types:
        return title

    return title + " ⋄ " + " <- ".join(inner_types)
