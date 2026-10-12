import json
from unittest.mock import patch

from django.test import TestCase as DjangoTestCase

from bugsink.app_settings import get_settings, override_settings as override_bugsink_settings
from events.factories import create_event
from issues.factories import get_or_create_issue
from projects.models import Project

from .service_backends.slack import SLACK_RED, build_alert_message, slack_backend_send_alert


class TestSettings(DjangoTestCase):

    def test_defaults_are_empty(self):
        self.assertEqual(get_settings().SLACK_BOT_TOKEN, "")
        self.assertEqual(get_settings().SLACK_SIGNING_SECRET, "")

    def test_can_be_overridden(self):
        with override_bugsink_settings(SLACK_BOT_TOKEN="xoxb-test"):
            self.assertEqual(get_settings().SLACK_BOT_TOKEN, "xoxb-test")


def section_texts(message):
    return [b["text"]["text"] for b in message["attachments"][0]["blocks"] if b["type"] == "section"]


class TestBuildAlertMessage(DjangoTestCase):

    def setUp(self):
        self.project = Project.objects.create(name="CAT Backend")
        self.issue, _ = get_or_create_issue(project=self.project)
        self.issue.calculated_type = "IllegalTransitionException"
        self.issue.calculated_value = 'Illegal transition "cancel"'
        self.url = get_settings().BASE_URL + self.issue.get_absolute_url()

    def test_full_message(self):
        self.issue.transaction = "GET /payment/finalize-transaction"
        self.issue.last_frame_filename = "src/Controller.php"
        self.issue.last_frame_module = "App\\Controller"
        self.issue.last_frame_function = "finalizeTransaction"

        message = build_alert_message(self.issue, "production", "New issue", buttons=True)

        self.assertEqual(
            message["text"],
            f"Event in production from CAT Backend in GET /payment/finalize-transaction (<{self.url}|details>)")
        self.assertEqual(section_texts(message), [
            '*New issue*\nIllegalTransitionException: Illegal transition "cancel"',
            "*Location*\nsrc/Controller.php - App\\Controller.finalizeTransaction",
        ])
        attachment = message["attachments"][0]
        self.assertEqual(attachment["color"], SLACK_RED)
        buttons = attachment["blocks"][-1]["elements"]
        self.assertEqual([b["action_id"] for b in buttons], ["resolve", "snooze", "ignore"])
        self.assertEqual({b["value"] for b in buttons}, {str(self.issue.id)})

    def test_minimal_message_has_no_location_and_no_buttons(self):
        message = build_alert_message(self.issue, "", "Regression")

        self.assertEqual(message["text"], f"Event from CAT Backend (<{self.url}|details>)")
        self.assertEqual(len(message["attachments"][0]["blocks"]), 1)

    def test_escapes_special_characters(self):
        self.issue.transaction = "GET /a?b=1&c=<x>"

        message = build_alert_message(self.issue, "", "New issue")

        self.assertIn("in GET /a?b=1&amp;c=&lt;x&gt; (", message["text"])

    def test_escapes_title(self):
        self.issue.calculated_value = "<!channel> & co"

        message = build_alert_message(self.issue, "", "New issue")

        self.assertIn("&lt;!channel&gt; &amp; co", section_texts(message)[0])

    def test_unmute_reason_is_added(self):
        message = build_alert_message(self.issue, "", "Unmuted issue", unmute_reason="Seen 5 times today")

        self.assertEqual(section_texts(message)[1], "Seen 5 times today")


class TestWebhookBackendUsesBuilder(DjangoTestCase):

    @patch("alerts.service_backends.base.BaseWebhookBackend.safe_post")
    def test_posts_attachment_with_latest_environment(self, mock_post):
        project = Project.objects.create(name="CAT Backend")
        issue, _ = get_or_create_issue(project=project)
        create_event(issue=issue, environment="production")
        config = project.service_configs.create(
            display_name="x", kind="slack", config=json.dumps({"webhook_url": "https://hooks.slack.com/test"}))

        slack_backend_send_alert("https://hooks.slack.com/test", str(issue.id), "New issue", "a", "NEW", config.id)

        data = json.loads(mock_post.call_args.kwargs["data"])
        self.assertIn("Event in production from CAT Backend", data["text"])
