from unittest import TestCase

from issues.utils import get_denormalized_fields_for_data
from sentry.at_glitchtip_af9a700a8706.stacktraces.processing import get_crash_frame_from_event_data


class CrashFrameFromEventDataTestCase(TestCase):
    def test_non_sequence_frames_are_ignored(self):
        data = {"exception": {"values": [{"stacktrace": {"frames": "[truncated:max-depth]"}}]}}

        self.assertIsNone(get_crash_frame_from_event_data(data))
        self.assertEqual("", get_denormalized_fields_for_data(data)["last_frame_filename"])

    def test_invalid_exception_frames_do_not_hide_top_level_frames(self):
        data = {
            "exception": {"values": [{"stacktrace": {"frames": "invalid"}}]},
            "stacktrace": {"frames": [{"filename": "top-level.py"}]},
        }

        self.assertEqual(
            {"filename": "top-level.py"},
            get_crash_frame_from_event_data(data),
        )

    def test_invalid_top_level_frames_fall_back_to_single_thread(self):
        data = {
            "stacktrace": {"frames": "invalid"},
            "threads": {"values": [{"stacktrace": {"frames": [{"filename": "thread.py"}]}}]},
        }

        self.assertEqual(
            {"filename": "thread.py"},
            get_crash_frame_from_event_data(data),
        )

    def test_non_mapping_frame_items_are_ignored(self):
        data = {"stacktrace": {"frames": [{"filename": "valid.py"}, "invalid", None]}}

        self.assertEqual(
            {"filename": "valid.py"},
            get_crash_frame_from_event_data(data, frame_filter=lambda frame: frame.get("filename") is not None),
        )


class FrontControllerLocationTestCase(TestCase):
    index = {"filename": "/public/index.php", "function": "require_once", "in_app": True}
    vendor = {"filename": "/vendor/shopware/core/CustomerException.php", "function": "badCredentials", "in_app": False}
    app = {"filename": "/src/Controller/TicketController.php", "function": "search", "in_app": True}

    def last_frame_filename(self, frames):
        data = {"platform": "php", "exception": {"values": [{"stacktrace": {"frames": frames}}]}}
        return get_denormalized_fields_for_data(data)["last_frame_filename"]

    def test_only_front_controller_in_app_uses_innermost_frame(self):
        self.assertEqual(self.vendor["filename"], self.last_frame_filename([self.index, self.vendor]))

    def test_app_frame_below_front_controller_is_kept(self):
        self.assertEqual(self.app["filename"], self.last_frame_filename([self.index, self.app, self.vendor]))

    def test_front_controller_as_only_frame_is_kept(self):
        self.assertEqual(self.index["filename"], self.last_frame_filename([self.index]))
