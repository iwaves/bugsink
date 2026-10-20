import datetime

from django.test import TestCase as DjangoTestCase

from bugsink.test_utils import TransactionTestCase25251 as TransactionTestCase
from compat.timestamp import format_timestamp
from events.factories import create_event_data
from ingest.views import BaseIngestAPIView
from issues.grouping_mechanisms import BUGSINK_GROUPING_V3, BUGSINK_GROUPING_V4
from issues.models import Grouping, Issue
from issues.utils import get_key_with_mechanism_for_data
from projects.models import Project


def get_grouping_key_for_data(data, mechanism=BUGSINK_GROUPING_V4):
    return get_key_with_mechanism_for_data(data, grouping_mechanism=mechanism).key


def frame(filename="/src/Lock.php", in_app=True, context_line="d", lineno=10):
    return {
        "filename": filename,
        "lineno": lineno,
        "in_app": in_app,
        "pre_context": ["a", "b", "c"],
        "context_line": context_line,
        "post_context": ["e", "f", "g", "h"],
    }


def exception_event_data(*exceptions):
    # exceptions as (type, value, frames) triples, root cause first and the main exception last, like SDKs send them
    return {"exception": {"values": [
        {"type": type_, "value": value, "stacktrace": {"frames": frames}} for type_, value, frames in exceptions
    ]}}


class GroupingV4TestCase(DjangoTestCase):

    def test_key_is_root_cause_type_file_and_code_around_the_line(self):
        data = exception_event_data(
            ("ApiLockException", "lock", [frame()]),
            ("RecoverableMessageHandlingException", "lock", [frame("/src/LockRunner.php")]),
        )

        self.assertEqual("ApiLockException ⋄ /src/Lock.php ⋄ a\nb\nc\nd\ne\nf\ng", get_grouping_key_for_data(data))

    def test_message_and_line_number_are_not_part_of_the_key(self):
        first = exception_event_data(("ApiLockException", 'resource "order_0a1b2c"', [frame(lineno=22)]))
        second = exception_event_data(("ApiLockException", 'resource "order_9f8e7d"', [frame(lineno=30)]))

        self.assertEqual(get_grouping_key_for_data(first), get_grouping_key_for_data(second))

    def test_different_code_gives_different_keys(self):
        first = exception_event_data(("ApiLockException", "lock", [frame(context_line="d")]))
        second = exception_event_data(("ApiLockException", "lock", [frame(context_line="x")]))

        self.assertNotEqual(get_grouping_key_for_data(first), get_grouping_key_for_data(second))

    def test_whitespace_is_ignored(self):
        first = exception_event_data(("ApiLockException", "lock", [frame(context_line="return new self($a);")]))
        second = exception_event_data(("ApiLockException", "lock", [frame(context_line="    return new self( $a );")]))

        self.assertEqual(get_grouping_key_for_data(first), get_grouping_key_for_data(second))

    def test_blank_lines_are_dropped(self):
        data = exception_event_data(("ApiLockException", "lock", [{**frame(), "pre_context": ["a", "  ", "c"]}]))

        self.assertEqual("ApiLockException ⋄ /src/Lock.php ⋄ a\nc\nd\ne\nf\ng", get_grouping_key_for_data(data))

    def test_innermost_in_app_frame_is_used(self):
        frames = [
            frame("/src/Controller.php"),
            frame("/src/CartProcessor.php"),
            frame("/vendor/shopware/core/CartPersister.php", in_app=False),
        ]

        key = get_grouping_key_for_data(exception_event_data(("CartException", "locked", frames)))

        self.assertIn(" ⋄ /src/CartProcessor.php ⋄ ", key)

    def test_entry_point_does_not_count_as_in_app(self):
        frames = [frame("/public/index.php"), frame("/vendor/symfony/routing/UrlMatcher.php", in_app=False)]

        key = get_grouping_key_for_data(exception_event_data(("NotFoundHttpException", "No route", frames)))

        self.assertIn(" ⋄ /vendor/symfony/routing/UrlMatcher.php ⋄ ", key)

    def test_without_source_context_the_key_equals_v3(self):
        data = exception_event_data(
            ("Inner", "v", [{"filename": "/app.js", "lineno": 1, "in_app": True}]),
            ("Main", "x", []),
        )

        self.assertEqual(get_grouping_key_for_data(data, BUGSINK_GROUPING_V3), get_grouping_key_for_data(data))

    def test_log_message_key_equals_v3(self):
        self.assertEqual("Log Message: foo %s", get_grouping_key_for_data({"logentry": {"message": "foo %s"}}))

    def test_default_fingerprint_expansion_uses_the_v4_key(self):
        data = exception_event_data(("ApiLockException", "lock", [frame()]))
        data["fingerprint"] = ["{{ default }}", "fixed string"]

        key_with_mechanism = get_key_with_mechanism_for_data(data, grouping_mechanism=BUGSINK_GROUPING_V4)

        self.assertEqual(
            "ApiLockException ⋄ /src/Lock.php ⋄ a\nb\nc\nd\ne\nf\ng ⋄ fixed string", key_with_mechanism.key)
        self.assertEqual(BUGSINK_GROUPING_V4, key_with_mechanism.mechanism)


class GroupingV4IngestTestCase(TransactionTestCase):

    def test_lock_errors_for_different_orders_share_one_issue(self):
        now = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.timezone.utc)
        project = Project.objects.create(
            name="v4", alert_on_new_issue=False, alert_on_regression=False, alert_on_unmute=False)

        for order_id in ["0a1b2c", "9f8e7d", "5c6d7e"]:
            message = f'Could not acquire lock for resource "iwvs_finalize_order_{order_id}"'
            event_data = {**create_event_data(), **exception_event_data(("ApiLockException", message, [frame()]))}
            BaseIngestAPIView().digest_event(
                event_metadata={
                    "event_id": event_data["event_id"],
                    "project_id": project.id,
                    "ingested_at": format_timestamp(now),
                },
                event_data=event_data,
                digested_at=now,
            )

        self.assertEqual(3, Issue.objects.get().digested_event_count)
        self.assertEqual({BUGSINK_GROUPING_V4}, set(Grouping.objects.values_list("grouping_mechanism", flat=True)))
