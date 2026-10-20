from django.db import migrations
from django.db.models import F
from django.utils import timezone


def switch_projects_to_v4(apps, schema_editor):
    Project = apps.get_model("projects", "Project")

    # previous_grouping_mechanism is assigned first: MySQL evaluates the SET clause left to right.
    Project.objects.exclude(grouping_mechanism="bugsink-v4").update(
        previous_grouping_mechanism=F("grouping_mechanism"),
        grouping_mechanism="bugsink-v4",
        grouping_mechanism_upgraded_at=timezone.now(),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0023_grouping_v4"),
    ]

    operations = [
        migrations.RunPython(switch_projects_to_v4, migrations.RunPython.noop),
    ]
