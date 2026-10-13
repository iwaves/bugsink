# Repeat alerts after a timeout (global / project / issue)

## Goal

1. An open, unmuted issue that was alerted before and occurs again after a timeout triggers the alert again.
2. The timeout (default 3600 seconds) is configurable in the app: globally, per project, per issue.
3. Opting out of repeats is done by muting the issue (existing feature).

## Decisions

* The timeout runs from the last alert sent for the issue (any kind: new, regression, unmuted, repeat), not from the
  last event. With a quiet-period reading a constantly failing issue would never repeat.
* Muted and resolved issues never repeat. No new "mute" concept: `Issue.is_muted` (Mute button, Slack Ignore button).
* Repeats go through the existing `_send_alert` fan-out: every service backend and email recipient gets them.
* Values are seconds. NULL (project, issue) inherits: issue, then project, then global. 0 means never repeat, and
  overrides the levels above it.
* The global default lives in the DB and is edited in the app, not in env vars or the config file.
* `Issue.last_alerted_at` NULL means "never alerted": nothing to repeat. No backfill, so there is no alert storm on
  upgrade; issues alerted before the upgrade start repeating after their next new/regression/unmute alert.
* Out of scope: team-level timeout, per-service-config toggle, REST API field and Slack buttons for the issue timeout,
  units other than seconds, caching the global value.

## Components

### Data
* `alerts.AlertSettings`: single-row model (pk=1), `repeat_timeout` PositiveIntegerField, default 3600.
  `AlertSettings.get()` is `get_or_create(pk=1)[0]`, so no data migration is needed.
* `Project.alert_repeat_timeout`, `Issue.alert_repeat_timeout`: nullable PositiveIntegerField.
* `Issue.last_alerted_at`: nullable DateTimeField.
* Migrations: alerts 0005, projects 0020, issues 0034.

### Effective timeout (`alerts/models.py`)
`effective_repeat_timeout(issue, project)`: `issue.alert_repeat_timeout`, else `project.alert_repeat_timeout`, else
`AlertSettings.get().repeat_timeout`. The global row is only read when both are NULL (one PK lookup; no cache, add a
short-TTL one only if profiling asks for it).

### Trigger (`digest_event`, `ingest/views.py`, last step of the `else` branch)
After the regression and unmute handling, so an event that just alerted cannot alert twice:

    if (issue.last_alerted_at is not None and not issue.is_resolved and not issue.is_muted
            and (timeout := effective_repeat_timeout(issue, project))
            and digested_at - issue.last_alerted_at >= timedelta(seconds=timeout)):
        delay_on_commit(send_repeat_alert, str(issue.id))
        issue.last_alerted_at = digested_at

`issue.save()` at the end of `digest_event` persists the stamp: no extra write. Stamping in the digest transaction
means a burst of events queues at most one repeat. A VBC unmute later in the same digest finds the issue muted here
and is not affected.

### Stamping the other alerts
`issue.last_alerted_at = digested_at` next to each queued alert, only when the alert is actually queued (so
`alert_on_*` is respected): new issue and regression in `digest_event`, and `IssueStateManager.unmute` next to
`send_unmute_alert.delay` (triggering event's `ingested_at`, parsed: on the in-memory event of `digest_event` it is
still the string the event was created from).

### Message (`alerts/tasks.py`)
`send_repeat_alert(issue_id)` calls `_send_alert(issue_id, "Recurring issue", "a", "RECURRING")`. Discord's colour map
already has an orange `RECURRING` entry; no backend change.

### UI
* **Global:** superuser-only page `alerts/settings/` (`user_passes_test(is_superuser)`), a ModelForm with the single
  field, wrapped in `atomic_for_request_method` like the other superuser views; template `alerts/alert_settings.html`; "Alerts" link in
  `theme/templates/theme/navigation.html` next to "Users", superusers only. (`/settings/` is read-only and can be
  disabled by `MINIMIZE_INFORMATION_EXPOSURE`, so it is not reused.)
* **Project:** `alert_repeat_timeout` on `ProjectForm` fields and `project_edit.html`/`project_new.html`; also in the
  three field lists and the optional-on-write `extra_kwargs` of `projects/serializers.py` (`allow_null`). Blank means
  inherit.
* **Issue:** a small form in the "Issue Key Info" box of the issue page (`issues/templates/issues/base.html`; the box
  is hidden below the `xl` breakpoint) posting `alert_repeat_timeout` to the same URL; `_handle_post` in
  `issues/views.py` validates it with `AlertRepeatTimeoutForm` (`issues/forms.py`, non-negative integer, blank saves
  NULL) without a new route. Invalid input is ignored, as for invalid issue actions. Blank means inherit.

## Testing

* Unit: `effective_repeat_timeout` (issue wins, project wins, global fallback, 0 at each level, NULL).
* Ingest integration (`ingest/tests.py`, repeat task mocked, `digested_at` passed explicitly): no repeat inside the
  timeout; repeat after it; none when muted, resolved, timeout 0, or `last_alerted_at` NULL; two events in a burst
  queue one repeat; new/regression/unmute alerts stamp `last_alerted_at`; no stamp when `alert_on_*` is off.
* `alerts/tests.py`: `send_repeat_alert` reaches the backends like the existing three alert tasks.
* Global page: non-superuser denied (GET and POST), superuser saves and the effective timeout changes.
* Project form and API accept, return and clear the field; issue form saves a value and blank.

## Rollout

The default of 3600 means issues alerted after the upgrade repeat hourly while they stay open and keep occurring, by
email too. Setting the global value to 0 on the new page restores the previous behaviour.
