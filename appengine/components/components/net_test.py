#!/usr/bin/env python
# Copyright 2015 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import collections
import logging
import sys
import unittest

from test_support import test_env
test_env.setup_test_env()

from google.appengine.api import urlfetch
from google.appengine.ext import ndb

from components import auth
from components import net
from test_support import test_case


Response = collections.namedtuple('Response', 'status_code content')


class NetTest(test_case.TestCase):
  def setUp(self):
    super(NetTest, self).setUp()
    self.mock(auth, 'get_access_token', lambda *_args: ('token', 0))
    self.mock(logging, 'warning', lambda *_args: None)
    self.mock(logging, 'error', lambda *_args: None)

  def mock_urlfetch(self, calls):
    @ndb.tasklet
    def mocked(**kwargs):
      if not calls:
        self.fail('Unexpected urlfetch call: %s' % kwargs)
      expected, response = calls.pop(0)
      defaults = {
        'deadline': 10,
        'follow_redirects': False,
        'headers': None,
        'method': 'GET',
        'payload': None,
        'validate_certificate': True,
      }
      defaults.update(expected)
      self.assertEqual(defaults, kwargs)
      if isinstance(response, Exception):
        raise response
      raise ndb.Return(response)
    self.mock(net, 'urlfetch_async', mocked)
    return calls

  def test_request_works(self):
    self.mock_urlfetch([
      ({
        'deadline': 123,
        'headers': {'Accept': 'text/plain', 'Authorization': 'Bearer token'},
        'method': 'POST',
        'payload': 'post body',
        'url': 'http://localhost/123?a=%3D&b=%26',
      }, Response(200, 'response body')),
    ])
    response = net.request(
        url='http://localhost/123',
        method='POST',
        payload='post body',
        params={'a': '=', 'b': '&'},
        headers={'Accept': 'text/plain'},
        scopes=['scope'],
        service_account_key=auth.ServiceAccountKey('a', 'b', 'c'),
        deadline=123,
        max_attempts=5)
    self.assertEqual('response body', response)

  def test_retries_transient_errors(self):
    self.mock_urlfetch([
      ({'url': 'http://localhost/123'}, urlfetch.Error()),
      ({'url': 'http://localhost/123'}, Response(408, 'clien timeout')),
      ({'url': 'http://localhost/123'}, Response(500, 'server error')),
      ({'url': 'http://localhost/123'}, Response(200, 'response body')),
    ])
    response = net.request('http://localhost/123', max_attempts=4)
    self.assertEqual('response body', response)

  def test_gives_up_retrying(self):
    self.mock_urlfetch([
      ({'url': 'http://localhost/123'}, Response(500, 'server error')),
      ({'url': 'http://localhost/123'}, Response(500, 'server error')),
      ({'url': 'http://localhost/123'}, Response(200, 'response body')),
    ])
    with self.assertRaises(net.Error):
      net.request('http://localhost/123', max_attempts=2)

  def test_404(self):
    self.mock_urlfetch([
      ({'url': 'http://localhost/123'}, Response(404, 'Not found')),
    ])
    with self.assertRaises(net.NotFoundError):
      net.request('http://localhost/123')

  def test_401(self):
    self.mock_urlfetch([
      ({'url': 'http://localhost/123'}, Response(401, 'Auth error')),
    ])
    with self.assertRaises(net.AuthError):
      net.request('http://localhost/123')

  def test_403(self):
    self.mock_urlfetch([
      ({'url': 'http://localhost/123'}, Response(403, 'Auth error')),
    ])
    with self.assertRaises(net.AuthError):
      net.request('http://localhost/123')

  def test_json_request_works(self):
    self.mock_urlfetch([
      ({
        'deadline': 123,
        'headers': {
          'Authorization': 'Bearer token',
          'Content-Type': 'application/json; charset=utf-8',
          'Header': 'value',
        },
        'method': 'POST',
        'payload': '{"key":"value"}',
        'url': 'http://localhost/123?a=%3D&b=%26',
      }, Response(200, '{"a":"b"}')),
    ])
    response = net.json_request(
        url='http://localhost/123',
        method='POST',
        payload={'key': 'value'},
        params={'a': '=', 'b': '&'},
        headers={'Header': 'value'},
        scopes=['scope'],
        service_account_key=auth.ServiceAccountKey('a', 'b', 'c'),
        deadline=123,
        max_attempts=5)
    self.assertEqual({'a': 'b'}, response)

  def test_json_bad_response(self):
    self.mock_urlfetch([
      ({'url': 'http://localhost/123'}, Response(200, 'not a json')),
    ])
    with self.assertRaises(net.Error):
      net.json_request('http://localhost/123')


if __name__ == '__main__':
  if '-v' in sys.argv:
    unittest.TestCase.maxDiff = None
  unittest.main()
