import json
import requests
from django.utils import timezone

from django import forms
from django.template.defaultfilters import truncatechars

from snappea.decorators import shared_task
from bugsink.app_settings import get_settings
from bugsink.transaction import immediate_atomic

from issues.models import Issue
from .base import BaseWebhookBackend
from .webhook_security import validate_webhook_url


class SlackConfigForm(forms.Form):
    webhook_url = forms.URLField(required=True, assume_scheme="https")

    # Slack does not support multi-channel webhooks, as per the docs:
    # > You cannot override the default channel (chosen by the user who installed your app), username, or icon when
    # > you're using incoming webhooks to post messages. Instead, these values will always inherit from the associated
    # > Slack app configuration.

    def __init__(self, *args, **kwargs):
        config = kwargs.pop("config", None)

        super().__init__(*args, **kwargs)
        if config:
            self.fields["webhook_url"].initial = config.get("webhook_url", "")

    def get_config(self):
        return {
            "webhook_url": self.cleaned_data.get("webhook_url"),
        }

    def clean_webhook_url(self):
        webhook_url = self.cleaned_data["webhook_url"]
        try:
            validate_webhook_url(webhook_url)
        except ValueError as e:
            raise forms.ValidationError(str(e)) from e
        return webhook_url


def _safe_markdown(text):
    # Slack assigns a special meaning to some characters, so we need to escape them
    # to prevent them from being interpreted as formatting/special characters.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("*", "\\*").replace("_", "\\_")


SLACK_RED = "#e01e5a"
SLACK_GREEN = "#2eb67d"

# action_id -> button label; the click endpoint (alerts/views.py) maps the same ids to issue actions
SLACK_BUTTONS = {"resolve": "Mark as fixed", "snooze": "Snooze 1hr", "ignore": "Ignore"}


def _escape(text):
    # the only characters Slack requires escaping in mrkdwn text
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mrkdwn(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def latest_environment(issue):
    event = issue.event_set.order_by("-digest_order").first()
    return event.environment if event else ""


def build_alert_message(issue, environment, state_description, unmute_reason=None, buttons=False, color=SLACK_RED):
    issue_url = get_settings().BASE_URL + issue.get_absolute_url()

    header = ["Event"]
    if environment:
        header.append(f"in {_escape(environment)}")
    header.append(f"from {_escape(issue.project.name)}")
    if issue.transaction:
        header.append(f"in {_escape(issue.transaction)}")
    header.append(f"(<{issue_url}|details>)")

    blocks = [_mrkdwn(" ".join(header)), _mrkdwn(f"*{_escape(state_description)}*\n{_escape(issue.title())}")]

    if unmute_reason:
        blocks.append(_mrkdwn(unmute_reason))

    if issue.last_frame_filename:
        function = ".".join(filter(None, [issue.last_frame_module, issue.last_frame_function]))
        location = issue.last_frame_filename + (f" - {function}" if function else "")
        blocks.append(_mrkdwn(f"*Location*\n{_escape(location)}"))

    if buttons:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": label}, "action_id": action_id,
             "value": str(issue.id)}
            for action_id, label in SLACK_BUTTONS.items()
        ]})

    return {"text": truncatechars(issue.title(), 150), "attachments": [{"color": color, "blocks": blocks}]}


def _store_failure_info(service_config_id, exception, response=None):
    """Store failure information in the MessagingServiceConfig with immediate_atomic"""
    from alerts.models import MessagingServiceConfig

    with immediate_atomic(only_if_needed=True):
        try:
            config = MessagingServiceConfig.objects.get(id=service_config_id)

            config.last_failure_timestamp = timezone.now()
            config.last_failure_error_type = type(exception).__name__
            config.last_failure_error_message = str(exception)

            # Handle requests-specific errors
            if response is not None:
                config.last_failure_status_code = response.status_code
                config.last_failure_response_text = response.text[:2000]  # Limit response text size

                # Check if response is JSON
                try:
                    json.loads(response.text)
                    config.last_failure_is_json = True
                except (json.JSONDecodeError, ValueError):
                    config.last_failure_is_json = False
            else:
                # Non-HTTP errors
                config.last_failure_status_code = None
                config.last_failure_response_text = None
                config.last_failure_is_json = None

            config.save()
        except MessagingServiceConfig.DoesNotExist:
            # Config was deleted while task was running
            pass


def _store_success_info(service_config_id):
    """Clear failure information on successful operation"""
    from alerts.models import MessagingServiceConfig

    with immediate_atomic(only_if_needed=True):
        try:
            config = MessagingServiceConfig.objects.get(id=service_config_id)
            config.clear_failure_status()
            config.save()
        except MessagingServiceConfig.DoesNotExist:
            # Config was deleted while task was running
            pass


@shared_task
def slack_backend_send_test_message(webhook_url, project_name, display_name, service_config_id):
    # See Slack's Block Kit Builder

    data = {"text": "Test message by Bugsink to test the webhook setup.",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "TEST issue",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Test message by Bugsink to test the webhook setup.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": "*project*: " + _safe_markdown(project_name),
                        },
                        {
                            "type": "mrkdwn",
                            "text": "*message backend*: " + _safe_markdown(display_name),
                        },
                    ]
                }
            ]}

    try:
        result = SlackBackend.safe_post(
            webhook_url,
            data=json.dumps(data),
            headers={"Content-Type": "application/json"},
        )

        result.raise_for_status()

        _store_success_info(service_config_id)
    except requests.RequestException as e:
        response = getattr(e, 'response', None)
        _store_failure_info(service_config_id, e, response)

    except Exception as e:
        _store_failure_info(service_config_id, e)


@shared_task
def slack_backend_send_alert(
        webhook_url, issue_id, state_description, alert_article, alert_reason, service_config_id, unmute_reason=None):

    issue = Issue.objects.get(id=issue_id)
    data = build_alert_message(issue, latest_environment(issue), state_description, unmute_reason)

    try:
        result = SlackBackend.safe_post(
            webhook_url,
            data=json.dumps(data),
            headers={"Content-Type": "application/json"},
        )

        result.raise_for_status()

        _store_success_info(service_config_id)
    except requests.RequestException as e:
        response = getattr(e, 'response', None)
        _store_failure_info(service_config_id, e, response)

    except Exception as e:
        _store_failure_info(service_config_id, e)


class SlackBackend(BaseWebhookBackend):
    def __init__(self, service_config):
        self.service_config = service_config

    @classmethod
    def get_form_class(cls):
        return SlackConfigForm

    def send_test_message(self):
        config = json.loads(self.service_config.config)
        slack_backend_send_test_message.delay(
            config["webhook_url"],
            self.service_config.project.name,
            self.service_config.display_name,
            self.service_config.id,
        )

    def send_alert(self, issue_id, state_description, alert_article, alert_reason, **kwargs):
        config = json.loads(self.service_config.config)
        slack_backend_send_alert.delay(
            config["webhook_url"],
            issue_id,
            state_description,
            alert_article,
            alert_reason,
            self.service_config.id,
            **kwargs,
        )
