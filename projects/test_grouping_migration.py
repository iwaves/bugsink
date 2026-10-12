import importlib

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from issues.grouping_mechanisms import BUGSINK_GROUPING_V1, BUGSINK_GROUPING_V2, BUGSINK_GROUPING_V3
from projects.models import Project

migration = importlib.import_module("projects.migrations.0022_grouping_v3_data")


class SwitchProjectsToV3TestCase(TestCase):

    def test_projects_on_older_mechanisms_switch_with_a_transition(self):
        v1 = Project.objects.create(name="v1", grouping_mechanism=BUGSINK_GROUPING_V1)
        v2 = Project.objects.create(name="v2", grouping_mechanism=BUGSINK_GROUPING_V2)
        before = timezone.now()

        migration.switch_projects_to_v3(apps, None)

        for project, previous in [(v1, BUGSINK_GROUPING_V1), (v2, BUGSINK_GROUPING_V2)]:
            project.refresh_from_db()
            self.assertEqual(BUGSINK_GROUPING_V3, project.grouping_mechanism)
            self.assertEqual(previous, project.previous_grouping_mechanism)
            self.assertGreaterEqual(project.grouping_mechanism_upgraded_at, before)

    def test_projects_already_on_v3_are_left_alone(self):
        project = Project.objects.create(name="v3", grouping_mechanism=BUGSINK_GROUPING_V3)

        migration.switch_projects_to_v3(apps, None)

        project.refresh_from_db()
        self.assertIsNone(project.previous_grouping_mechanism)
        self.assertIsNone(project.grouping_mechanism_upgraded_at)
