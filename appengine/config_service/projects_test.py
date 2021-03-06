#!/usr/bin/env python
# Copyright 2015 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import test_env
test_env.setup_test_env()

import mock

from test_support import test_case

from proto import service_config_pb2
from proto import project_config_pb2
import projects
import storage


class ProjectsTestCase(test_case.TestCase):
  def setUp(self):
    super(ProjectsTestCase, self).setUp()
    self.mock(storage, 'get_latest', mock.Mock())

  def test_get_projects(self):
    storage.get_latest.return_value = '''
      projects {
        id: "chromium"
        config_storage_type: GITILES
        config_location: "http://localhost"
      }
    '''
    expected = service_config_pb2.ProjectsCfg(
        projects=[
          service_config_pb2.Project(
              id='chromium',
              config_storage_type=service_config_pb2.Project.GITILES,
              config_location='http://localhost'),
        ],
    )
    self.assertEqual(projects.get_projects(), expected.projects)

  def test_get_branches(self):
    storage.get_latest.return_value = '''
      branches {
        name: "master"
      }
      branches {
        name: "release42"
        config_path: "other"
      }
    '''
    expected = project_config_pb2.BranchesCfg(
        branches=[
          project_config_pb2.BranchesCfg.Branch(
              name='master'),
          project_config_pb2.BranchesCfg.Branch(
              name='release42', config_path='other'),
        ],
    )
    self.assertEqual(projects.get_branches('chromium'), expected.branches)

  def test_get_branches_of_non_existent_project(self):
    storage.get_latest.return_value = None
    self.assertEqual(projects.get_branches('chromium'), None)

  def test_repo_info(self):
    self.assertEqual(projects.get_repo('x'), (None, None))
    projects.update_import_info(
        'x', projects.RepositoryType.GITILES, 'http://localhost/x')
    # Second time for coverage.
    projects.update_import_info(
        'x', projects.RepositoryType.GITILES, 'http://localhost/x')
    self.assertEqual(
        projects.get_repo('x'),
        (projects.RepositoryType.GITILES, 'http://localhost/x'))

    # Change it
    projects.update_import_info(
        'x', projects.RepositoryType.GITILES, 'http://localhost/y')


if __name__ == '__main__':
  test_env.main()
