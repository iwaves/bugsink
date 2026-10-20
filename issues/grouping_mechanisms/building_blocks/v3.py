from collections.abc import Mapping

from sentry.at_glitchtip_af9a700a8706.utils.safe import get_path

from .v1 import get_values

# SDKs mark front controllers as in_app; keying on them would merge everything thrown in the code they call.
ENTRY_POINTS = ("/public/index.php", "/bin/console", "/artisan")


def get_root_cause(data):
    """The innermost exception of the chain, i.e. the first one, as SDKs send the root cause first."""
    values = get_values(get_path(data, "exception")) or []
    return values[0] if values and isinstance(values[0], Mapping) else None


def get_grouping_frame(exception):
    """The innermost in-app frame that is not an entry point, else the crash frame."""
    frames = get_path(exception, "stacktrace", "frames")
    frames = [
        frame for frame in (frames if isinstance(frames, list) else [])
        if isinstance(frame, Mapping) and not str(frame.get("filename") or "").endswith(ENTRY_POINTS)
    ]
    in_app_frames = [frame for frame in frames if frame.get("in_app")]
    return (in_app_frames or frames or [None])[-1]


def get_code_window(frame):
    """The source lines L-3 to L+3 around the frame's line as Bugsnag uses them: whitespace removed and blank lines
    dropped, so that reformatting does not matter."""
    if frame is None or not isinstance(frame.get("context_line"), str):
        return None

    pre_context = frame.get("pre_context") if isinstance(frame.get("pre_context"), list) else []
    post_context = frame.get("post_context") if isinstance(frame.get("post_context"), list) else []
    lines = ["".join(str(line).split()) for line in pre_context[-3:] + [frame["context_line"]] + post_context[:3]]
    return "\n".join(line for line in lines if line)
