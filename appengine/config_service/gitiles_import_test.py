#!/usr/bin/env python
# Copyright 2015 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import os

import test_env
test_env.setup_test_env()

from google.appengine.api import urlfetch_errors
from google.appengine.ext import ndb

import mock

from components import auth
from components import gitiles
from test_support import test_case

from proto import project_config_pb2
from proto import service_config_pb2
import gitiles_import
import projects
import storage
import validation


TEST_ARCHIVE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'test_archive.tar.gz')


class GitilesImportTestCase(test_case.TestCase):
  def test_get_gitiles_config_corrupted(self):
    self.mock(storage, 'get_latest', mock.Mock())
    storage.get_latest.return_value = 'garbage'
    gitiles_import.get_gitiles_config()

  def mock_get_archive(self):
    self.mock(gitiles, 'get_archive', mock.Mock())
    with open(TEST_ARCHIVE_PATH, 'r') as test_archive_file:
      gitiles.get_archive.return_value = test_archive_file.read()

  def test_import_revision(self):
    self.mock_get_archive()

    gitiles_import.import_revision(
        'config_set',
        gitiles.Location(
            hostname='localhost',
            project='project',
            treeish='a1841f40264376d170269ee9473ce924b7c2c4e9',
            path='/',
        ),
        create_config_set=True)

    gitiles.get_archive.assert_called_once_with(
        'localhost', 'project', 'a1841f40264376d170269ee9473ce924b7c2c4e9', '/',
        deadline=15)
    saved_config_set = storage.ConfigSet.get_by_id('config_set')
    self.assertIsNotNone(saved_config_set)
    self.assertEqual(
        saved_config_set.latest_revision,
        'a1841f40264376d170269ee9473ce924b7c2c4e9')

    saved_revision = storage.Revision.get_by_id(
        'a1841f40264376d170269ee9473ce924b7c2c4e9', parent=saved_config_set.key)
    self.assertIsNotNone(saved_revision)

    saved_file = storage.File.get_by_id(
        'test_archive/x', parent=saved_revision.key)
    self.assertIsNotNone(saved_file)
    self.assertEqual(
        saved_file.content_hash, 'v1:587be6b4c3f93f93c489c0111bba5596147a26cb')

    saved_blob = storage.Blob.get_by_id(saved_file.content_hash)
    self.assertIsNotNone(saved_blob)
    self.assertEqual(saved_blob.content, 'x\n')

    # Run second time, assert nothing is fetched from gitiles.
    ndb.Key(storage.ConfigSet, 'config_set').delete()
    gitiles.get_archive.reset_mock()
    gitiles_import.import_revision(
        'config_set',
        gitiles.Location(
            hostname='localhost',
            project='project',
            treeish='a1841f40264376d170269ee9473ce924b7c2c4e9',
            path='/'),
        create_config_set=True)
    self.assertFalse(gitiles.get_archive.called)

  def test_import_revision_no_acrhive(self):
    self.mock(gitiles, 'get_archive', mock.Mock(return_value=None))

    gitiles_import.import_revision(
        'config_set',
        gitiles.Location(
          hostname='localhost',
          project='project',
          treeish='a1841f40264376d170269ee9473ce924b7c2c4e9',
          path='/'))

  def test_import_invalid_revision(self):
    self.mock_get_archive()
    self.mock(validation, 'validate_config', mock.Mock(return_value=False))

    gitiles_import.import_revision(
        'config_set',
        gitiles.Location(
          hostname='localhost',
          project='project',
          treeish='a1841f40264376d170269ee9473ce924b7c2c4e9',
          path='/'))
    # Assert not saved.
    self.assertIsNone(storage.ConfigSet.get_by_id('config_set'))

  def mock_get_log(self):
    latest_commit = gitiles.Commit(
        sha='a1841f40264376d170269ee9473ce924b7c2c4e9',
        tree='deadbeef',
        parents=['beefdead'],
        author=None,
        committer=None,
        message=None)
    self.mock(gitiles, 'get_log', mock.Mock())
    gitiles.get_log.return_value = gitiles.Log(
        commits=[latest_commit],
    )

  def test_import_config_set(self):
    self.mock_get_log()
    self.mock_get_archive()

    gitiles_import.import_config_set(
        'config_set', gitiles.Location.parse('https://localhost/project'))

    gitiles.get_log.assert_called_once_with(
        'localhost', 'project', 'HEAD', '/', limit=1,
        deadline=15)

    saved_config_set = storage.ConfigSet.get_by_id('config_set')
    self.assertIsNotNone(saved_config_set)
    self.assertEqual(
        saved_config_set.latest_revision,
        'a1841f40264376d170269ee9473ce924b7c2c4e9')
    self.assertTrue(storage.Revision.get_by_id(
        'a1841f40264376d170269ee9473ce924b7c2c4e9',
        parent=saved_config_set.key))

    # Import second time, import_revision should not be called.
    self.mock(gitiles_import, 'import_revision', mock.Mock())
    gitiles_import.import_config_set(
        'config_set', gitiles.Location.parse('https://localhost/project'))
    self.assertFalse(gitiles_import.import_revision.called)

  def test_import_config_set_with_log_failed(self):
    self.mock(gitiles_import, 'import_revision', mock.Mock())
    self.mock(gitiles, 'get_log', mock.Mock(return_value = None))
    gitiles_import.import_config_set(
        'config_set',
        gitiles.Location.parse('https://localhost/project'))

  def test_import_config_set_with_auth_error(self):
    def raise_auth_error(*_, **__):
      raise auth.AuthorizationError()
    self.mock(gitiles, 'get_log', mock.Mock(side_effect=raise_auth_error))

    # Should not raise an exception.
    gitiles_import.import_config_set(
        'config_set',
        gitiles.Location.parse('https://localhost/project'))

  def test_deadline_exceeded(self):
    self.mock_get_log()

    def raise_deadline_exceeded(*args, **kwrags):
      raise urlfetch_errors.DeadlineExceededError()
    self.mock(gitiles, 'get_archive', mock.Mock())
    gitiles.get_archive.side_effect = raise_deadline_exceeded

    # Should not raise an exception.
    gitiles_import.import_config_set(
        'config_set',
        gitiles.Location.parse('https://localhost/project'))

  def test_import_services(self):
    self.mock(gitiles_import, 'import_config_set', mock.Mock())
    self.mock(gitiles, 'get_tree', mock.Mock())
    gitiles.get_tree.return_value = gitiles.Tree(
        id='abc',
        entries=[
          gitiles.TreeEntry(
              id='deadbeef',
              name='luci-config',
              type='tree',
              mode=0,
          ),
          gitiles.TreeEntry(
              id='deadbeef1',
              name='malformed service id',
              type='tree',
              mode=0,
          ),
        ],
    )

    gitiles_import.import_services(
        gitiles.Location.parse('https://localhost/config'))

    gitiles.get_tree.assert_called_once_with(
        'localhost', 'config', 'HEAD', '/')
    gitiles_import.import_config_set.assert_called_once_with(
        'services/luci-config',
        'https://localhost/config/+/HEAD/luci-config')

  def test_import_projects_and_branches(self):
    self.mock(gitiles_import, 'import_config_set', mock.Mock())
    self.mock(projects, 'get_projects', mock.Mock())
    self.mock(projects, 'get_branches', mock.Mock())
    projects.get_projects.return_value = [
      service_config_pb2.Project(
          id='chromium',
          config_location='https://localhost/chromium/src/',
          config_storage_type=service_config_pb2.Project.GITILES,
      ),
      service_config_pb2.Project(
          id='bad_location',
          config_location='https://localhost/',
          config_storage_type=service_config_pb2.Project.GITILES,
      ),
      service_config_pb2.Project(
          id='non-gitiles',
      ),
    ]
    BranchType = project_config_pb2.BranchesCfg.Branch
    projects.get_branches.return_value = [
      BranchType(name='master'),
      BranchType(name='release42', config_path='/my-configs'),
    ]

    gitiles_import.import_projects()

    self.assertEqual(gitiles_import.import_config_set.call_count, 3)
    gitiles_import.import_config_set.assert_any_call(
        'projects/chromium', 'https://localhost/chromium/src/+/luci')
    gitiles_import.import_config_set.assert_any_call(
        'projects/chromium/branches/master',
        'https://localhost/chromium/src/+/master/luci')
    gitiles_import.import_config_set.assert_any_call(
        'projects/chromium/branches/release42',
        'https://localhost/chromium/src/+/release42/my-configs')

  def test_import_projects_exception(self):
    self.mock(gitiles_import, 'import_project', mock.Mock())
    gitiles_import.import_project.side_effect = Exception

    self.mock(projects, 'get_projects', mock.Mock())
    projects.get_projects.return_value = [
      service_config_pb2.Project(
          id='chromium',
          config_location='https://localhost/chromium/src/',
          config_storage_type=service_config_pb2.Project.GITILES,
      ),
      service_config_pb2.Project(
          id='will-fail',
          config_location='https://localhost/chromium/src/',
          config_storage_type=service_config_pb2.Project.GITILES,
      )
    ]

    gitiles_import.import_projects()
    self.assertEqual(gitiles_import.import_project.call_count, 2)


if __name__ == '__main__':
  test_env.main()
