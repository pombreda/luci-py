# Copyright 2014 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

"""Grab bag file for transaction."""

import logging

from google.appengine.api import datastore_errors
from google.appengine.ext import ndb
from google.appengine.ext.ndb import tasklets
from google.appengine.runtime import apiproxy_errors


__all__ = [
  'CommitError',
  'transaction',
  'transaction_async',
]


class CommitError(Exception):
  """A transaction probably failed but it may or may not have occurred.

  The caller may want to run a second transaction to verify if the previous one
  succeeded.
  """


@ndb.tasklet
def transaction_async(callback, **ctx_options):
  """Converts all sorts of random exceptions into CommitError.

  Arguments:
    callback: function to run in the transaction. See
        https://cloud.google.com/appengine/docs/python/ndb/functions for more
        details.

  Sets retries default value to 1 instead 3 (!)
  """
  ctx_options.setdefault('retries', 1)
  try:
    result = yield ndb.transaction_async(callback, **ctx_options)
    raise ndb.Return(result)
  except (
      datastore_errors.InternalError,
      datastore_errors.Timeout,
      datastore_errors.TransactionFailedError) as e:
    # https://cloud.google.com/appengine/docs/python/datastore/transactions
    # states the result is ambiguous, it could have succeeded.
    logging.info('Transaction likely failed: %s', e)
    raise CommitError(e)
  except (
      apiproxy_errors.CancelledError,
      datastore_errors.BadRequestError,
      RuntimeError) as e:
    logging.info('Transaction failure: %s', e)
    raise CommitError(e)


def transaction(callback, **ctx_options):
  """Synchronous version of transaction_async()."""
  future = transaction_async(callback, **ctx_options)
  return future.get_result()


@ndb.utils.decorator
def transactional_async(func, args, kwds, **ctx_options):
  """The async version of @txn.transactional."""
  if args or kwds:
    return transaction_async(lambda: func(*args, **kwds), **ctx_options)
  return transaction_async(func, **ctx_options)


@ndb.utils.decorator
def transactional(func, args, kwds, **ctx_options):
  """Decorator that wraps a function with txn.transaction."""
  return transactional_async.wrapped_decorator(
      func, args, kwds, **ctx_options).get_result()


@ndb.utils.decorator
def transactional_tasklet(func, args, kwds, **options):
  """The tasklet version of @txn.transactional_async."""
  func = tasklets.tasklet(func)
  return transactional_async.wrapped_decorator(func, args, kwds, **options)
