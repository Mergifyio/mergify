.. meta::
   :description: Mergify Documentation for Queues rules
   :keywords: mergify, queues

.. _Queue rules:

==============
🔀 Queue Rules
==============

|premium plan tag|
|beta tag|

``queues_rules`` are used by the ``queue`` action. They define the conditions
to get a queued pull request merged and the configuration of the merge
behavior.

The ``queues_rules`` and the ``queue`` action allow writing complex merge queue
workflows that couldn't be achieved with the ``merge`` action.

The Merge Train
~~~~~~~~~~~~~~~


When using the ``merge`` action with strict mode, pull requests are updated and
merged one by one serially.

On the other hand, queues allow pull request to get embarked into a merge
train. Mergify creates temporary pull requests to embedding multiple pull
requests at once to check if the resulting branch can be merged according to
your ``queue_rules```.

For example, let's say the base branch head commit is ``abcdef12`` and there are 3 pull
requests, ``A``, ``B`` and ``C``.
As soon as the 3 pull requests match the conditions for the queue action they get embarked together.
Mergify therefore creates the following pull requests:

* ``W`` with the content of ``abcdef12`` + ``A``
* ``X`` with the content of ``abcdef12`` + ``A`` + ``B``
* ``Y`` with the content of ``abcdef12`` + ``A`` + ``B`` + ``C``

The upside of creating multiple pull requests is that continuous integration
pipelines can run on all of them in parallel. Currently, Mergify embarks up to
5 pull requests into the merge train.

When the pull request ``W`` matches the ``queue_rules``, the pull request ``A``
is merged and the pull request ``W`` is deleted. The same goes for pull request
``X`` and ``B``, as well as for the pull request ``Y`` and ``C``.

At this point, let's say that ``A`` and ``B`` have been merged successfully.

If a 4th pull request ``D`` is created and matches the condition for the queue
action, it is also embarked into the merge train. Since ``B`` have been merged
in the base branch, the new head commit for the base branch is ``12345678``.

In that case, Mergify creates a new pull request ``Z`` with the content of
``12345678`` + ``C`` + ``D``.

In the case where a new commit is pushed on the base branch, Mergify resets the
merge train and starts over the process.

If an embarked pull request doesn't match the ``queue_rules`` anymore, it
is removed from the merge train. All pull requests embarked after it
are disembarked and re-embarked.

Working with Queue Rules
~~~~~~~~~~~~~~~~~~~~~~~~

With a ``queue`` action, a pull request has to match two sets of conditions
to get merged. The first set of conditions described in ``pull_request_rules``
allows the pull request to enter in the queue when all conditions match. If
the ``queue`` action conditions do not match anymore the pull request is
removed from the merge queue. When a pull request is at the top of the queue,
Mergify looks at the seconds set of conditions defined this time in
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
     - The name of the merge queue.
   * - ``conditions``
     - list of ``conditions``
     -
     - The list of ``conditions`` to match to get the queued pull request merged.

.. include:: global-substitutions.rst
