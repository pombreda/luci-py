#!/usr/bin/python2.7
#
# Copyright 2011 Google Inc. All Rights Reserved.

"""Drives test on the Swarm server with a test machine provider."""




import cookielib
import httplib
import json
import logging
import optparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib
import urllib2
import urlparse

from common import swarm_constants
from common import url_helper

# Number of seconds to sleep between tries of polling for results.
SLEEP_BETWEEN_RESULT_POLLS = 2

ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')

# The script to start the slave with. The python script is passed in because
# during tests, sys.executable was sometimes failing to find python.
START_SLAVE = """import os
import subprocess
import sys

subprocess.call(['%(python)s', '%(slave_script)s',
                 '-a', '%(server_address)s', '-p', '%(server_port)s',
                 '-d', '%(slave_directory)s',
                 '-l', os.path.join('%(slave_directory)s', 'slave.log'),
                 '%(config_file)s'])
"""


def CopyDirectory(source_path, destination_path):
  """Copy a directory like shutil.copy_tree, but without keeping stats.

  This will allow making a copy of a protected build folder that can then
  be modified.

  Args:
    source_path: The directory to copy over.
    destination_path: The destination directory to copy the files to.
  """
  for item in os.listdir(source_path):
    if os.path.isdir(os.path.join(source_path, item)):
      destination_subdirectory = os.path.join(destination_path, item)
      os.mkdir(destination_subdirectory)

      CopyDirectory(os.path.join(source_path, item),
                    destination_subdirectory)
    else:
      shutil.copyfile(os.path.join(source_path, item),
                      os.path.join(destination_path, item))


# TODO(user): Find a way to avoid spawning other processes so that we can
# get code coverage, which doesn't work for code in other processes in Python.
class _ProcessWrapper(object):
  """This class controls the life span of a child process.

  Attributes:
    command_line: An array with the command to execute followed by its args.
  """

  def __init__(self, command_line, cwd=None):
    self.wrapped_process = subprocess.Popen(command_line, cwd=cwd)

  def __del__(self):
    if hasattr(self, 'wrapped_process') and self.wrapped_process.poll() is None:
      self.wrapped_process.terminate()


class _SwarmTestCase(unittest.TestCase):
  """Test case class for Swarm integration tests."""

  def GetAdminUrl(self, url=None):
    """"Returns an url to login an admin user and then go to a url (if given).

    Args:
      url: The url to continue to.

    Returns:
      The full url.
    """
    admin_url = urlparse.urljoin(self._swarm_server_url, '_ah/login?'
                                 'email=john@doe.com&admin=True&action=Login')

    if url:
      admin_url += '&continue=' + urllib2.quote(url)

    return admin_url

  def setUp(self):
    self._swarm_server_process = None
    self._slave_machine_process = None

    swarm_server_addr = 'http://localhost'
    swarm_server_port = '8181'
    self._swarm_server_url = '%s:%s' % (swarm_server_addr, swarm_server_port)

    # TODO(user): Eventually add --datastore_consistency_policy=random
    # to ensure we are properly handling datastore inconsistency. This should be
    # possible once this project lives fully in the open source world.
    _SwarmTestProgram.options.appengine_cmds.extend(
        ['-c', '-p %s' % swarm_server_port, '--skip_sdk_update_check',
         _SwarmTestProgram.options.swarm_path])

    self._swarm_server_process = _ProcessWrapper(
        _SwarmTestProgram.options.appengine_cmds)
    logging.info('Started Swarm server Process with pid: %s',
                 self._swarm_server_process.wrapped_process.pid)

    # Setup the slave code in a temporary directory so it can be modified.
    self._temp_directory = tempfile.mkdtemp()
    slave_root_dir = os.path.join(
        os.path.dirname(_SwarmTestProgram.options.slave_script), '..')
    CopyDirectory(slave_root_dir, self._temp_directory)

    # Move the slave_machine.py script to its correct home.
    os.rename(os.path.join(self._temp_directory,
                           swarm_constants.TEST_RUNNER_DIR,
                           swarm_constants.SLAVE_MACHINE_SCRIPT),
              os.path.join(self._temp_directory,
                           swarm_constants.SLAVE_MACHINE_SCRIPT))

    # Remove the local test runner script to ensure the slave is out of date
    # and is updated.
    local_test_runner_script = os.path.join(self._temp_directory,
                                            swarm_constants.TEST_RUNNER_DIR,
                                            swarm_constants.TEST_RUNNER_SCRIPT)
    os.remove(local_test_runner_script)

    # Create the start_slave.py script.
    start_slave_script = os.path.join(self._temp_directory,
                                      'start_slave.py')
    full_start_slave_script = START_SLAVE % {
        'python': sys.executable,
        'slave_script': os.path.join(self._temp_directory,
                                     swarm_constants.SLAVE_MACHINE_SCRIPT),
        'server_address': swarm_server_addr,
        'server_port': swarm_server_port,
        'slave_directory': self._temp_directory,
        'config_file': _SwarmTestProgram.options.slave_config_file}
    with open(start_slave_script, 'w') as f:
      f.write(full_start_slave_script)

    # Wait for the Swarm server to be ready
    ready = False
    time_out = _SwarmTestProgram.options.swarm_server_start_timeout
    started = time.time()
    while not ready and time_out > time.time() - started:
      try:
        urllib2.urlopen(self._swarm_server_url)
        ready = True
      except urllib2.URLError:
        time.sleep(1)
    self.assertTrue(ready, 'The swarm server could not be started')

    # Whitelist the machine to be allowed to run tests.
    cj = cookielib.CookieJar()
    opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj))
    # Make this opener the default so we can use url_helper.UrlOpen and still
    # have the cookies present.
    urllib2.install_opener(opener)

    opener.open(self.GetAdminUrl())

    opener.open(urlparse.urljoin(self._swarm_server_url,
                                 'secure/change_whitelist'),
                urllib.urlencode({'a': True}))

    # Upload the start slave script to the server.
    url_helper.UrlOpen(urlparse.urljoin(self._swarm_server_url,
                                        'secure/upload_start_slave'),
                       files=[('script', 'script', full_start_slave_script)],
                       method='POSTFORM')

    # Start the slave machine script to start polling for tests.
    logging.info('Current dir: %s', os.getcwd())
    slave_script = os.path.join(self._temp_directory,
                                swarm_constants.TEST_RUNNER_DIR,
                                'slave_machine.py')
    logging.info('Trying to run slave script: %s',
                 slave_script)

    slave_machine_cmds = [
        sys.executable,
        start_slave_script]
    if _SwarmTestProgram.options.verbose:
      slave_machine_cmds.append('-v')
    self._slave_machine_process = _ProcessWrapper(slave_machine_cmds,
                                                  self._temp_directory)

    logging.info('Started slave machine process with pid: %s',
                 self._slave_machine_process.wrapped_process.pid)

  def tearDown(self):
    shutil.rmtree(self._temp_directory)
    try:
      logging.info('Quitting Swarm server')
      cj = cookielib.CookieJar()
      opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj))
      opener.open(self.GetAdminUrl(urlparse.urljoin(self._swarm_server_url,
                                                    'tasks/quitquitquit')))
      # TODO(user): This line shouldn't be here, should be after the
      # try block.
      self._swarm_server_process.wrapped_process.wait()
    except (httplib.BadStatusLine, urllib2.HTTPError):
      # We expect this to throw since the quitquitquit request caused the
      # server to terminate and thus it won't be able to complete the request.
      pass

  def testHandlingSwarmFiles(self):
    """This test sends a series of Swarm files to the server."""
    swarm_files = []
    logging.info('Walking %s, looking for Swarm files.',
                 os.path.join(os.environ['TEST_SRCDIR'],
                              _SwarmTestProgram.options.tests_path))
    for dirpath, _, filenames in os.walk(os.path.join(
        os.environ['TEST_SRCDIR'], _SwarmTestProgram.options.tests_path)):
      for filename in filenames:
        if os.path.splitext(filename)[1].lower() == '.swarm':
          swarm_files.append(os.path.join(dirpath, filename))

    self.assertTrue(swarm_files, 'No swarm files found to test, considering '
                    'this a failure since the end-to-end process won\'t be '
                    'tested')

    logging.info('Will send these files to Swarm server: %s', swarm_files)

    swarm_server_test_url = urlparse.urljoin(self._swarm_server_url, 'test')
    swarm_server_get_matching_test_cases = urlparse.urljoin(
        self._swarm_server_url, 'get_matching_test_cases')
    running_tests = []
    for swarm_file in swarm_files:
      logging.info('Sending content of %s to Swarm server.', swarm_file)
      # Build the URL for sending the request.
      test_request = open(swarm_file)
      output = None
      try:
        data = urllib.urlencode({'request': test_request.read()})
        output = urllib2.urlopen(swarm_server_test_url, data=data).read()
      except urllib2.URLError as ex:
        self.fail('Error: %s' % str(ex))

      # Check that we can read the output as a JSON string
      try:
        test_keys = json.loads(output)
        logging.info('Test successfully uploaded')
      except (ValueError, TypeError) as e:
        self.fail('Swarm Request failed: %s' % output)

      logging.info(test_keys)

      current_test_keys = []
      for test_key in test_keys['test_keys']:
        current_test_keys.append(test_key['test_key'])
        running_tests.append(test_key)
        if _SwarmTestProgram.options.verbose:
          logging.info('Config: %s, index: %s/%s, test key: %s',
                       test_key['config_name'],
                       int(test_key['instance_index']) + 1,
                       test_key['num_instances'],
                       test_key['test_key'])

      # Make sure that we can actually find the keys from just the test names.
      logging.info('Checking if we can find the keys for the recently added '
                   'tests')

      data = urllib.urlencode({'name': test_keys['test_case_name']})
      # Append the data to the url so the request is a GET request as
      # required.
      matching_keys_json = urllib2.urlopen(
          swarm_server_get_matching_test_cases + '?' + data)
      matching_keys = json.load(matching_keys_json)
      self.assertEquals(set(matching_keys), set(current_test_keys))

    # TODO(user): This code below and a big chunk of the parts above were stolen
    # from the post_test.py script under the tools folder. We should find a
    # way to extract and reuse it somehow.
    test_result_output = ''
    # TODO(user): Maybe collect all failures so that we can enumerate them at
    # the end as the local test runner and gtest does.

    cj = cookielib.CookieJar()
    opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj))
    urllib2.install_opener(opener)

    # The slave machine is running along with this test. Thus it may take
    # some time before all the tests complete. We will keep polling the results
    # with delays between them. If after 10 times all the tests are still not
    # completed, we report a failure.
    triggered_retry = False
    for _ in range(10):
      logging.info('Still waiting for the following tests:\n%s',
                   '\n'.join(test['test_key'] for test in running_tests))

      for running_test_key in running_tests[:]:
        key_url = self.GetAdminUrl(
            urlparse.urljoin(
                self._swarm_server_url,
                'secure/get_result?r=' + running_test_key['test_key']))

        logging.info('Opening URL %s', key_url)
        try:
          output = urllib2.urlopen(key_url).read()
        except urllib2.HTTPError as e:
          self.fail('Calling %s threw %s' % (key_url, e))

        results = json.loads(output)

        if not results['exit_codes']:
          # The test hasn't finished yet
          continue

        logging.info('Test done for %s', running_test_key['config_name'])

        if '0 FAILED TESTS' not in output:
          self.fail('Test failed.\n%s' % output)

        # If we haven't retried a runner yet, do that with this runner.
        if not triggered_retry:
          logging.info('Retrying test %s', running_test_key['test_key'])
          swarm_server_retry_url = urlparse.urljoin(self._swarm_server_url,
                                                    'secure/retry')
          data = urllib.urlencode({'r': running_test_key['test_key']})
          urllib2.urlopen(swarm_server_retry_url, data=data)
          triggered_retry = True
        else:
          running_tests.remove(running_test_key)

        test_result_output = (
            '%s\n=======================\nConfig: %s\n%s' %
            (test_result_output, running_test_key['config_name'], output))
        logging.info(test_result_output)
        logging.info('=======================')

      if running_tests:
        logging.info('At least one test not yet succeeded')
        time.sleep(SLEEP_BETWEEN_RESULT_POLLS)
      else:
        logging.info('All tests succeeded')
        break

    if running_tests:
      self.fail('%d tests failed to complete.' % len(running_tests))


class _SwarmTestProgram(unittest.TestProgram):
  """The Swarm specific test program so that we can have specialised options."""

  # Command line options
  options = None

  # TODO(user): Find a way to also display base class command line args for -h.
  _DESCRIPTION = ('This script starts a Swarm server with a test machine '
                  'provider and runs a server mocking provided machines.')

  def parseArgs(self, argv):
    """Overloaded from base class so we can add our own options."""
    parser = optparse.OptionParser(usage='%prog [options] [filename]',
                                   description=_SwarmTestProgram._DESCRIPTION)
    parser.add_option('-a', '--appengine', dest='appengine_cmds',
                      action='append', help='The command(s) to start the '
                      'AppEngine launcher. The -c, -p, and '
                      '--skip_sdk_update_check arguments will '
                      'be added to the command(s) you specify.')
    parser.add_option('-s', '--swarm', dest='swarm_path',
                      default=ROOT_DIR,
                      help='The root path of the Swarm server code.')
    parser.add_option('-t', '--tests', dest='tests_path',
                      default=os.path.join(ROOT_DIR, 'tests/test_files'),
                      help='The path where the test files can be found.')
    parser.add_option('-c', '--slave-config', dest='slave_config_file',
                      default=os.path.join(ROOT_DIR, 'tests',
                                           'machine_config.txt'),
                      help='The path to the slave dimensions config file. '
                      'Defaults to %default.')
    parser.add_option('-l', '--slave-script', dest='slave_script',
                      default=os.path.join(ROOT_DIR, 'swarm_bot',
                                           'slave_machine.py'),
                      help='The path to the slave_machine.py script. '
                      'Defaults to %default.')
    parser.add_option('-o', '--swarm_server_start_timeout',
                      dest='swarm_server_start_timeout', type=int,
                      default=90,
                      help='How long should we wait (in seconds) for the '
                      'Swarm server to start? Defaults to %default seconds.')
    parser.add_option('-v', '--verbose', action='store_true',
                      help='Set logging level to INFO. Optional. Defaults to '
                      'ERROR level.')

    (_SwarmTestProgram.options, other_args) = parser.parse_args(args=argv[1:])
    if not _SwarmTestProgram.options.appengine_cmds:
      parser.error('You must specify the AppEngine command(s) to start the '
                   'AppEngine launcher.')

    if _SwarmTestProgram.options.verbose:
      logging.getLogger().setLevel(logging.INFO)
    else:
      logging.getLogger().setLevel(logging.ERROR)

    test_srcdir = os.environ['TEST_SRCDIR']
    if test_srcdir:
      _SwarmTestProgram.options.appengine_cmds[0] = os.path.join(
          test_srcdir, _SwarmTestProgram.options.appengine_cmds[0])
      _SwarmTestProgram.options.swarm_path = os.path.join(
          test_srcdir, _SwarmTestProgram.options.swarm_path)
      _SwarmTestProgram.options.slave_script = os.path.join(
          test_srcdir, _SwarmTestProgram.options.slave_script)
      _SwarmTestProgram.options.slave_config_file = os.path.join(
          test_srcdir, _SwarmTestProgram.options.slave_config_file)
    super(_SwarmTestProgram, self).parseArgs(other_args)


if __name__ == '__main__':
  if 'TEST_SRCDIR' not in os.environ:
    os.environ['TEST_SRCDIR'] = ''
  _SwarmTestProgram()
