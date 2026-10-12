from django.test import TestCase as DjangoTestCase

from bugsink.app_settings import get_settings, override_settings as override_bugsink_settings


class TestSettings(DjangoTestCase):

    def test_defaults_are_empty(self):
        self.assertEqual(get_settings().SLACK_BOT_TOKEN, "")
        self.assertEqual(get_settings().SLACK_SIGNING_SECRET, "")

    def test_can_be_overridden(self):
        with override_bugsink_settings(SLACK_BOT_TOKEN="xoxb-test"):
            self.assertEqual(get_settings().SLACK_BOT_TOKEN, "xoxb-test")
