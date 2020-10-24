.. meta::
   :description: Mergify Documentation for Queues rules
   :keywords: mergify, queues

.. _Queue rules:

==============
🔀 Queue Rules
==============

|premium plan tag|

``queues_rules`` are used by the ``queue`` action. They define the conditions
to get a queued pull request merged and the configuration of the merge
behavior.

The ``queues_rules`` and the ``queue`` action allow writing complex merge queue
workflow that can't be done with the ``merge`` action.

Working with Queue Rules
~~~~~~~~~~~~~~~~~~~~~~~~

With a ``queue`` action, a pull request has to match two sets of conditions
to get merged. The first set of conditions described in ``pull_request_rules``
allows the pull request to enter in the merge queue if all conditions match. If
the ``queue`` action conditions do not match anymore the pull request is
removed from the merge queue. Then when a pull request is the next one to be
merged, Mergify looks at the seconds set of conditions defined this time in
``queue_rules`` and wait that they all match to merge the pull request.

When multiple queues are defined, they are processed one after the other in the
order they are defined in the configuration. When the first pull request in the
first queue is waiting to be merged, the remaining queues are blocked.

Example
~~~~~~~

In the following example, a pull request needs the CI to pass and 2 reviews to
enter the queue. If the ``urgent`` label is set, the pull request enters in the
queue only with 2 reviews, bypassing any pull requests in the default queue.
The urgent queue still ensures the CI passes before merging the pull request.


.. code-block:: yaml

    queue_rules:
      - name: urgent
        conditions:
          - check-success=Travis CI - Pull Request

      - name: default
        conditions:


    pull_request_rules:
      - name: move to urgent queue when 2 reviews and label urgent
        conditions:
          - base=master
          - "#approved-reviews-by>=2"
          - label=urgent
        actions:
          queue:
            name: urgent

      - name: move to default queue when CI passes and 2 reviews
        conditions:
          - base=master
          - "#approved-reviews-by>=2"
          - check-success=Travis CI - Pull Request
        actions:
          queue:
            name: default



Queue Rule Parameters
~~~~~~~~~~~~~~~~~~~~~

A ``queue_rules`` takes the following parameter:

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``name``
     - string
     -
     - The name of the merge queue
   * - ``conditions``
     - list of ``conditions``
     -
     - The list of ``conditions`` to match to get the queued pull request merged.

   * - ``merge_bot_account``
     - string
     -
     - Mergify can impersonate a GitHub user to merge pull request.
       If no ``merge_bot_account`` is set, Mergify will merge the pull request
       itself. The user account **must** have already been
       logged in Mergify dashboard once.

.. include:: global-substitutions.rst
