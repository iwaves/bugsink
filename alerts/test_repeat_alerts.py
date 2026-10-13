from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from bugsink.test_utils import TransactionTestCase25251 as TransactionTestCase
from events.factories import create_event
from issues.factories import get_or_create_issue
from projects.models import Project, ProjectMembership

from .models import AlertSettings, effective_repeat_timeout
from .tasks import send_repeat_alert

User = get_user_model()


class TestEffectiveRepeatTimeout(TestCase):

    def setUp(self):
        self.project = Project.objects.create(name="Test project")
        self.issue, _ = get_or_create_issue(project=self.project)

    def effective(self, global_=None, project=None, issue=None):
        if global_ is not None:
            AlertSettings.objects.update_or_create(pk=1, defaults={"repeat_timeout": global_})
        self.project.alert_repeat_timeout = project
        self.issue.alert_repeat_timeout = issue
        return effective_repeat_timeout(self.issue, self.project)

    def test_global_default_is_one_hour(self):
        self.assertEqual(3600, self.effective())

    def test_global_from_db(self):
        self.assertEqual(60, self.effective(global_=60))

    def test_project_overrides_global(self):
        self.assertEqual(120, self.effective(global_=60, project=120))

    def test_issue_overrides_project(self):
        self.assertEqual(240, self.effective(global_=60, project=120, issue=240))

    def test_zero_is_a_value_not_inherit(self):
        self.assertEqual(0, self.effective(global_=60, project=0))
        self.assertEqual(0, self.effective(global_=60, project=120, issue=0))
        self.assertEqual(60, self.effective(global_=60, project=None, issue=None))


class TestSendRepeatAlert(TestCase):

    def test_send_repeat_alert_mails_project_members(self):
        project = Project.objects.create(name="Test project")
        user = User.objects.create_user(username="testuser", email="test@example.org")
        ProjectMembership.objects.create(project=project, user=user, send_email_alerts=True, accepted=True)
        issue, _ = get_or_create_issue(project=project)
        create_event(project=project, issue=issue)

        send_repeat_alert(issue.id)

        self.assertEqual(1, len(mail.outbox))
        self.assertIn("Recurring issue", mail.outbox[0].subject)


class TestAlertSettingsView(TransactionTestCase):

    def setUp(self):
        self.url = reverse("alert_settings")
        self.superuser = User.objects.create_user(username="admin", email="admin@example.org", is_superuser=True)
        self.user = User.objects.create_user(username="testuser", email="test@example.org")

    def test_non_superuser_is_denied(self):
        self.client.force_login(self.user)

        self.assertEqual(302, self.client.get(self.url).status_code)
        self.assertEqual(302, self.client.post(self.url, {"repeat_timeout": 60}).status_code)
        self.assertEqual(3600, AlertSettings.get().repeat_timeout)

    def test_superuser_sees_current_value(self):
        self.client.force_login(self.superuser)

        response = self.client.get(self.url)

        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'value="3600"')

    def test_superuser_saves_value(self):
        self.client.force_login(self.superuser)

        response = self.client.post(self.url, {"repeat_timeout": 0})

        self.assertEqual(302, response.status_code)
        self.assertEqual(0, AlertSettings.get().repeat_timeout)

    def test_invalid_value_is_not_saved(self):
        self.client.force_login(self.superuser)

        response = self.client.post(self.url, {"repeat_timeout": -5})

        self.assertEqual(200, response.status_code)
        self.assertEqual(3600, AlertSettings.get().repeat_timeout)

    def test_nav_link_for_superusers_only(self):
        self.client.force_login(self.superuser)
        self.assertContains(self.client.get(reverse("preferences")), 'href="/alerts/settings/"')

        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse("preferences")), 'href="/alerts/settings/"')
