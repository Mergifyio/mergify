.. meta::
   :description: Mergify Documentation for Request Review Action
   :keywords: mergify, request, review, pull request
   :summary: Request review from users or teams for a pull request.
   :doc:icon: binoculars

.. _request_reviews action:

request_reviews
===============

.. note::

   GitHub does not allow to request more than 15 users or teams for a review.

The ``request_reviews`` action requests reviews from users for the pull
request.

Options
-------

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

Examples
--------

👀 Flexible Reviewers Assignment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

You can assign people for review based on any criteria you like. A classic is
to use the name of modified files to do it:

.. code-block:: yaml

    pull_request_rules:
      - name: ask jd to review changes on python files
        conditions:
          - files~=\.py$
          - -closed
        actions:
          request_reviews:
            users:
              - jd

You can also ask entire teams to review a pull request based on, e.g., labels:

.. code-block:: yaml

    pull_request_rules:
      - name: ask the security team to review security labelled PR
        conditions:
          - label=security
        actions:
          request_reviews:
            teams:
              - "@myorg/security-dev"
              - "@myorg/security-ops"


👘 Random Review Assignment
~~~~~~~~~~~~~~~~~~~~~~~~~~~

|premium plan tag|
|essential plan tag|

It's not fair to ask for the same users or teams to always do the review. You
can rather randomly assign a pull request to a group of users.

.. code-block:: yaml

    pull_request_rules:
      - name: ask the security team to review security labelled PR
        conditions:
          - label=security
        actions:
          request_reviews:
            users:
              - jd
              - sileht
              - CamClrt
              - GuillaumeOj
           random_count: 2

In that case, 2 users from this list of 4 users will get a review requested for
this pull request.

If you prefer some users to have a larger portion of the pull requests
assigned, you can add a weight to the user list:

.. code-block:: yaml

    pull_request_rules:
      - name: ask the security team to review security labelled PR
        conditions:
          - label=security
        actions:
          request_reviews:
            users:
              jd: 2
              sileht: 3
              CamClrt: 1
              GuillaumeOj: 1
          random_count: 2

In that case, it's 3 times more likely then the user ``sileht`` will get a pull
request assigned rather than ``GuillaumeOj``.

.. include:: ../global-substitutions.rst
