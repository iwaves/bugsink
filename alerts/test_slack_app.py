import hashlib
import hmac
import json
import time
from unittest.mock import Mock, patch

from django.test import TestCase as DjangoTestCase
from django.utils import timezone

from bugsink.app_settings import get_settings, override_settings as override_bugsink_settings
from events.factories import create_event
from issues.factories import get_or_create_issue
from projects.models import Project

from .models import MessagingServiceConfig
from .service_backends.slack import SLACK_RED, build_alert_message, slack_backend_send_alert
from .service_backends.slack_app import SlackAppConfigForm, verify_slack_signature


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


TOKEN = "xoxb-test"


class TestSlackAppConfigForm(DjangoTestCase):

    def test_requires_bot_token(self):
        form = SlackAppConfigForm(data={"channel": "#errors"})

        self.assertFalse(form.is_valid())
        self.assertIn("SLACK_BOT_TOKEN", form.errors["channel"][0])

    def test_valid_with_token(self):
        with override_bugsink_settings(SLACK_BOT_TOKEN=TOKEN):
            form = SlackAppConfigForm(data={"channel": "#errors"})

            self.assertTrue(form.is_valid())
            self.assertEqual(form.get_config(), {"channel": "#errors"})


class TestSlackAppSending(DjangoTestCase):

    def setUp(self):
        self.project = Project.objects.create(name="CAT Backend")
        self.config = MessagingServiceConfig.objects.create(
            project=self.project, display_name="#errors", kind="slack_app", config=json.dumps({"channel": "#errors"}))
        self.issue, _ = get_or_create_issue(project=self.project)

    @patch("alerts.service_backends.slack_app.requests.post")
    def test_alert_is_posted_to_channel_with_bot_token(self, mock_post):
        mock_post.return_value = Mock(json=lambda: {"ok": True})
        self.config.last_failure_timestamp = timezone.now()
        self.config.save()

        with override_bugsink_settings(SLACK_BOT_TOKEN=TOKEN):
            self.config.get_backend().send_alert(str(self.issue.id), "New issue", "a", "NEW")

        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://slack.com/api/chat.postMessage")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer xoxb-test")
        self.assertEqual(kwargs["json"]["channel"], "#errors")
        self.assertEqual(kwargs["json"]["attachments"][0]["blocks"][-1]["type"], "actions")
        self.config.refresh_from_db()
        self.assertIsNone(self.config.last_failure_timestamp)

    @patch("alerts.service_backends.slack_app.requests.post")
    def test_slack_error_is_stored_as_failure(self, mock_post):
        mock_post.return_value = Mock(json=lambda: {"ok": False, "error": "channel_not_found"})

        with override_bugsink_settings(SLACK_BOT_TOKEN=TOKEN):
            self.config.get_backend().send_alert(str(self.issue.id), "New issue", "a", "NEW")

        self.config.refresh_from_db()
        self.assertEqual(self.config.last_failure_error_type, "SlackAPIError")
        self.assertEqual(self.config.last_failure_error_message, "channel_not_found")

    @patch("alerts.service_backends.slack_app.requests.post")
    def test_test_message_is_posted_to_channel(self, mock_post):
        mock_post.return_value = Mock(json=lambda: {"ok": True})

        with override_bugsink_settings(SLACK_BOT_TOKEN=TOKEN):
            self.config.get_backend().send_test_message()

        self.assertEqual(mock_post.call_args.kwargs["json"]["channel"], "#errors")
        self.assertIn("Test message by Bugsink", mock_post.call_args.kwargs["json"]["text"])


SECRET = "signing-secret"


def sign(body, secret=SECRET, timestamp=None):
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    digest = hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    return timestamp, "v0=" + digest


class TestVerifySlackSignature(DjangoTestCase):
    body = b"payload=%7B%7D"

    def test_valid(self):
        timestamp, signature = sign(self.body)
        self.assertTrue(verify_slack_signature(SECRET, timestamp, self.body, signature))

    def test_wrong_secret(self):
        timestamp, signature = sign(self.body, secret="other")
        self.assertFalse(verify_slack_signature(SECRET, timestamp, self.body, signature))

    def test_tampered_body(self):
        timestamp, signature = sign(self.body)
        self.assertFalse(verify_slack_signature(SECRET, timestamp, self.body + b"x", signature))

    def test_stale_timestamp(self):
        timestamp, signature = sign(self.body, timestamp=int(time.time()) - 301)
        self.assertFalse(verify_slack_signature(SECRET, timestamp, self.body, signature))

    def test_unset_secret_never_verifies(self):
        timestamp, signature = sign(self.body, secret="")
        self.assertFalse(verify_slack_signature("", timestamp, self.body, signature))

    def test_missing_or_garbled_headers(self):
        self.assertFalse(verify_slack_signature(SECRET, None, self.body, None))
        self.assertFalse(verify_slack_signature(SECRET, "not-a-number", self.body, "v0=abc"))
        timestamp, _ = sign(self.body)
        self.assertFalse(verify_slack_signature(SECRET, timestamp, self.body, "v0=é"))
