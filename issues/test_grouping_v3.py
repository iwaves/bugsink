import datetime

from django.test import TestCase as DjangoTestCase

from bugsink.test_utils import TransactionTestCase25251 as TransactionTestCase
from compat.timestamp import format_timestamp
from events.factories import create_event_data
from ingest.views import BaseIngestAPIView
from issues.grouping_mechanisms import BUGSINK_GROUPING_V2, BUGSINK_GROUPING_V3
from issues.models import Grouping, Issue
from issues.utils import get_key_with_mechanism_for_data as _get_key_with_mechanism_for_data
from projects.models import Project


def get_key_with_mechanism_for_data(data, mechanism=BUGSINK_GROUPING_V3):
    return _get_key_with_mechanism_for_data(data, grouping_mechanism=mechanism)


def get_grouping_key_for_data(data, mechanism=BUGSINK_GROUPING_V3):
    return get_key_with_mechanism_for_data(data, mechanism).key


def exception_event_data(*exceptions):
    # exceptions as (type, value) pairs, innermost (root cause) first and the main exception last, like SDKs send them
    return {"exception": {"values": [{"type": type_, "value": value} for type_, value in exceptions]}}


class GroupingV3TestCase(DjangoTestCase):

    def test_inner_exception_type_is_part_of_the_key(self):
        data = exception_event_data(("InvalidTokenStructure", "no dots"), ("OAuthServerException", "denied"))

        self.assertEqual("OAuthServerException: denied ⋄ InvalidTokenStructure", get_grouping_key_for_data(data))

    def test_inner_exception_types_are_listed_nearest_first(self):
        data = exception_event_data(("Root", "r"), ("Middle", "m"), ("Main", "x"))

        self.assertEqual("Main: x ⋄ Middle <- Root", get_grouping_key_for_data(data))

    def test_inner_exception_values_are_not_part_of_the_key(self):
        first = exception_event_data(("Inner", "first value"), ("Main", "x"))
        second = exception_event_data(("Inner", "second value"), ("Main", "x"))

        self.assertEqual(get_grouping_key_for_data(first), get_grouping_key_for_data(second))

    def test_different_inner_exception_types_give_different_keys(self):
        first = exception_event_data(("InnerA", "v"), ("Main", "x"))
        second = exception_event_data(("InnerB", "v"), ("Main", "x"))
        without = exception_event_data(("Main", "x"))

        keys = {get_grouping_key_for_data(d) for d in [first, second, without]}

        self.assertEqual(3, len(keys))

    def test_without_inner_exceptions_the_key_equals_v2(self):
        data = exception_event_data(("KeyError", "Cannot publish <Consumer object at 0x7fbb00112233>"))

        self.assertEqual(
            get_grouping_key_for_data(data, BUGSINK_GROUPING_V2),
            get_grouping_key_for_data(data),
        )

    def test_main_exception_value_is_normalized_as_in_v2(self):
        first = exception_event_data(("Inner", "v"), ("Main", "request took 123ms"))
        second = exception_event_data(("Inner", "v"), ("Main", "request took 456ms"))

        self.assertEqual("Main: request took <duration> ⋄ Inner", get_grouping_key_for_data(first))
        self.assertEqual(get_grouping_key_for_data(first), get_grouping_key_for_data(second))

    def test_inner_exception_type_is_trimmed_like_the_main_type(self):
        data = exception_event_data(("I" * 200, "v"), ("Main", "x"))

        self.assertEqual("Main: x ⋄ " + "I" * 127 + "…", get_grouping_key_for_data(data))

    def test_log_message_has_no_inner_exception_types(self):
        self.assertEqual("Log Message: foo %s", get_grouping_key_for_data({"logentry": {"message": "foo %s"}}))

    def test_default_fingerprint_expansion_includes_inner_exception_types(self):
        data = exception_event_data(("Inner", "v"), ("Main", "x"))
        data["fingerprint"] = ["{{ default }}", "fixed string"]

        key_with_mechanism = get_key_with_mechanism_for_data(data)

        self.assertEqual("Main: x ⋄ Inner ⋄ fixed string", key_with_mechanism.key)
        self.assertEqual(BUGSINK_GROUPING_V3, key_with_mechanism.mechanism)


class GroupingV3IngestTestCase(TransactionTestCase):

    def digest(self, project, exceptions, now):
        event_data = {**create_event_data(), **exception_event_data(*exceptions)}
        BaseIngestAPIView().digest_event(
            event_metadata={
                "event_id": event_data["event_id"],
                "project_id": project.id,
                "ingested_at": format_timestamp(now),
            },
            event_data=event_data,
            digested_at=now,
        )

    def create_project(self, **kwargs):
        return Project.objects.create(
            name="v3", alert_on_new_issue=False, alert_on_regression=False, alert_on_unmute=False, **kwargs)

    def test_same_exception_with_different_inner_exceptions_creates_separate_issues(self):
        now = datetime.datetime(2026, 10, 12, 12, 0, tzinfo=datetime.timezone.utc)
        project = self.create_project()
        main = ("OAuthServerException", "denied")

        self.digest(project, [("RequiredConstraintsViolated", "expired"), main], now)
        self.digest(project, [("InvalidTokenStructure", "no dots"), main], now)
        self.digest(project, [main], now)
        self.digest(project, [("RequiredConstraintsViolated", "expired again"), main], now)

        self.assertEqual(3, Issue.objects.count())
        self.assertEqual([1, 1, 2], sorted(Issue.objects.values_list("digested_event_count", flat=True)))
        self.assertEqual({BUGSINK_GROUPING_V3}, set(Grouping.objects.values_list("grouping_mechanism", flat=True)))

    def test_existing_v2_issue_keeps_receiving_events_during_the_transition(self):
        now = datetime.datetime(2026, 10, 12, 12, 0, tzinfo=datetime.timezone.utc)
        project = self.create_project(grouping_mechanism=BUGSINK_GROUPING_V2)
        main = ("OAuthServerException", "denied")

        self.digest(project, [("RequiredConstraintsViolated", "expired"), main], now)
        old_issue = Issue.objects.get()

        project.grouping_mechanism = BUGSINK_GROUPING_V3
        project.previous_grouping_mechanism = BUGSINK_GROUPING_V2
        project.grouping_mechanism_upgraded_at = now
        project.save()

        self.digest(project, [("RequiredConstraintsViolated", "expired"), main], now + datetime.timedelta(minutes=1))
        self.digest(project, [("InvalidTokenStructure", "no dots"), main], now + datetime.timedelta(minutes=2))

        # during the transition, every variant is attached to the pre-existing issue through the v2 key
        self.assertEqual(1, Issue.objects.count())
        self.assertEqual(3, Issue.objects.get().digested_event_count)
        self.assertEqual({old_issue.id}, set(Grouping.objects.values_list("issue_id", flat=True)))
        self.assertEqual(
            {BUGSINK_GROUPING_V2, BUGSINK_GROUPING_V3},
            set(Grouping.objects.values_list("grouping_mechanism", flat=True)),
        )
