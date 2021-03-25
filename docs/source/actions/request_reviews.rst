.. meta::
   :description: Mergify Documentation for Request Review Action
   :keywords: mergify, request, review, pull request
   :summary: Request review from users or teams for a pull request.
   :doc:icon: binoculars

.. _request_reviews action:

request_reviews
===============

The ``request_reviews`` action requests reviews from users for the pull
request.

.. list-table::
  :header-rows: 1
  :widths: 1 1 1 2

  * - Key Name
    - Value Type
    - Default
    - Value Description
  * - ``users``
    - list of string or dictionary of login and weight
    -
    - The username to request reviews from.
  * - ``teams``
    - list of string or dictionary of login and weight
    -
    - The team name to request reviews from.
  * - ``random_count``
    - integer between 1 and 15
    -
    - |premium plan tag| |essential plan tag|
      Pick random users and teams from the provided lists. When
      ``random_count`` is specified, ``users`` and ``teams`` can be a
      dictionary where the key is the login and the value is the weight to use.
      Weight must be between 1 and 65535 included.

.. note::

   GitHub does not allow to request more 15 users or teams for a review.

.. include:: ../global-substitutions.rst
