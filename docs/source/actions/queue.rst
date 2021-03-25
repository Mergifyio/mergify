.. meta::
   :description: Mergify Documentation for Queue Action
   :keywords: mergify, queue, pull request
   :summary: Put a pull request in queue before merging.
   :doc:icon: train

.. _queue action:

queue
=====

|beta tag|

The ``queue`` action moves the pull request into one of the merge queue defined
in :ref:`queue rules`.

Merge queues prevent merging broken pull requests by serializing their merge.
Merging broken pull requests can happen when outdated pull requests are being
merged in their base branch.

Why Queues?
-----------

To understand the problem, imagine the following situation:

- The base branch (e.g., ``main``) has its continuous integration testing
  passing correctly.

- A pull request is created, which also passes the CI.

The state of the repository can be represented like this:

.. graphviz:: strict-mode-master-pr-ci-pass.dot
   :alt: Pull request is open

While the pull request is open, another commit is pushed to ``main`` — let's
call it `new commit`. That can be a local commit or a merge commit from another
pull request; who knows. The tests are run against ``main`` by the CI and
they pass. The state of the repository and its continuous integration system
can be described like this:

.. graphviz:: strict-mode-new-master-pr-ci-pass.dot
   :alt: Base branch adds a new commit

The pull request is still marked as valid by the continuous integration system
since it did not change. As there is no code conflict, the pull request is
considered as `mergeable` by GitHub: the merge button is green.

If you click that merge button, this is what `might` happen:

.. graphviz:: strict-mode-merge-ci-fail.dot
   :alt: Base branch is broken

As a new merge commit is created to merge the pull request, it is possible that
the continuous integration testing fails. Indeed, the continuous integration
did not test the pull request with the `new commit` that has been added to the
base branch. Some new test might have been introduced by this `new commit` in
the base branch while the pull request was open. That pull request may not have
the correct code to pass this new test.

Using Queues
------------

Using a merge queue solves that issue by updating any pull request that is not
up-to-date with its base branch before being merged. That forces the continuous
integration system to test again the pull request with the new code from its
base branch.

In the previous example, if a merge queue was being used, Mergify would have
merged ``main`` in the base branch automatically. The continuous integration
system would have run again and marked the pull request as failing the test,
removing it from the merge queue altogether.

.. graphviz:: strict-mode-rebase-ci-fail.dot
   :alt: Rebase make CI fails

When multiple pull requests are mergeable, they are scheduled to be merged
sequentially, and they will be updated on top of each other.

The pull request branch update is only done when the pull request is ready to
be merged by the engine, e.g., when all the `conditions` are validated.

That means that when a first pull request has been merged, and the second one
is outdated like this:

.. graphviz:: queue-strict-merge-pr2-open.dot
   :alt: strict merge with PR2 open

Mergify will make sure the pull request #2 is updated with the latest tip of
the base branch before merging:

.. graphviz:: queue-strict-merge.dot
   :alt: strict merge


Configuring the Merge Queues
----------------------------

The `merge queues` rely on two configuration items: the ``queues_rules`` and
the ``queue`` action. This allows writing complex merge queue workflows that
couldn't be achieved with the ``merge`` action alone.

The ``queue`` action places the pull request inside the merge queue: the set of
conditions described in the ``pull_request_rules`` allows the pull request to
enter the queue when all conditions match.

If the ``queue`` action conditions do not match anymore, the pull request is
removed from the merge queue.

When a pull request is at the top of the queue, Mergify looks at the second set
of conditions defined this time in ``queue_rules`` and wait that they all match
to merge the pull request.

When multiple queues are defined, they are processed one after the other in the
order they are defined in the configuration. Queue processing is blocked until
all preceding queues, or higher priorities queues are empty.

.. _speculative merges:

Speculative Merges
------------------

|premium plan tag|

Merging pull requests one by one serially can take a lot of time, depending on
the continuous integration run time. To merge pull requests faster, Mergify
queues support `speculative merges`.

With speculative merges, the first pull requests from the queue are embarked in
a `merge train` and tested together in parallel so they can be merged faster. A
merge train consists of two or more pull requests embarked together to be
tested speculatively.

The upside of creating multiple temporary pull requests is that continuous
integration pipelines can run on all of them in parallel. Currently, Mergify
embarks up from 1 to 20 pull requests into the merge train (You can set the
value with the ``speculative_checks`` settings in ``queue_rules``).

.. graphviz:: queue-strict-train.dot
   :alt: Merge train

In the above example, three pull requests are tested at the same time by the
continuous integration system: ``PR #1``, ``PR #1 + PR #2`` and ``PR #1 + PR #2
+ PR#3``. By creating temporary pull requests that combine multiple pull
requests, Mergify is able to schedule in advance the testing of the combined
results of every pull request in the queue.

Those temporary pull requests check if the resulting branch can be merged
according to the ``queue_rules`` conditions. If any of the merge car fails to
match the ``queue_rules`` conditions, the culprit pull request is removed from
the queue.

When the pull request ``PR #1 + PR #2`` matches the ``queue_rules`` conditions,
the pull requests ``PR #1`` and ``PR #2`` are merged and the temporary pull
request ``PR #1 + PR #2`` is deleted. The same goes for the pull request ``PR
#1 + PR #2 + PR #3`` and ``PR #3``.

In the case where a new commit is pushed on the base branch, Mergify resets the
merge train and starts over the process.

If an embarked pull request doesn't match the ``queue_rules`` anymore, it is
removed from the merge train. All pull requests embarked after it are
disembarked and re-embarked.

Configuration
-------------

Queue Action
~~~~~~~~~~~~

These are the options of the ``queue`` action:

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
     - The name of the merge queue where to move the pull request.
   * - ``method``
     - string
     - ``merge``
     - Merge method to use. Possible values are ``merge``, ``squash`` or
       ``rebase``.
   * - ``rebase_fallback``
     - string
     - ``merge``
     - If ``method`` is set to ``rebase``, but the pull request cannot be
       rebased, the method defined in ``rebase_fallback`` will be used instead.
       Possible values are ``merge``, ``squash``, ``none``. ``none`` will
       report an error if rebase is not possible.
   * - ``merge_bot_account``
     - string
     -
     - |premium plan tag|
       Mergify can impersonate a GitHub user to merge pull request.
       If no ``merge_bot_account`` is set, Mergify will merge the pull request
       itself. The user account **must** have already been
       logged in Mergify dashboard once and have **write** or **maintain** permission.

   * - ``priority``
     - 1 <= integer <= 10000 or ``low`` or ``medium`` or ``high``
     - ``medium``
     - |premium plan tag| This sets the priority of the pull request in the queue. The pull
       request with the highest priority is merged first.
       ``low``, ``medium``, ``high`` are aliases for ``1000``, ``2000``, ``3000``.

   * - ``commit_message``
     - string
     - ``default``
     - Defines what commit message to use when merging using the ``squash`` or
       ``merge`` method. Possible values are:

       * ``default`` to use the default commit message provided by GitHub
         or defined in the pull request body (see :ref:`commit message`).

       * ``title+body`` means to use the title and body from the pull request
         itself as the commit message. The pull request number will be added to
         end of the title.

.. _queue rules:

Queue Rules
~~~~~~~~~~~

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
   * - ``speculative_checks``
     - int
     - 1
     - |premium plan tag| The maximum number of checks to run in parallel in the queue. Must be
       between 1 and 20.
       See :ref:`speculative merges`.


Example
-------

A basic usage of the queue is to merge pull requests serially, ensuring they
all pass the CI. The following example defines a queue named ``default`` which
allows any pull request to be merged once it entered.

To enter the queue, the pull requests need to be based on the ``main`` branch,
have 2 approving reviews and pass the CI.

.. code-block:: yaml

    queue_rules:
      - name: default
        conditions:


    pull_request_rules:
      - name: merge using the merge queue
        conditions:
          - base=main
          - "#approved-reviews-by>=2"
          - check-success=Travis CI - Pull Request
        actions:
          queue:
            name: default

By using multiple queues, it's possible to put some pull requests in a higher
priorities queue. As queues are processed one after the other, the following
example allow to add a label ``urgent`` to a pull request so it gets put in the
higher priority queue.

.. code-block:: yaml

    queue_rules:
      - name: urgent
        conditions:

      - name: default
        conditions:


    pull_request_rules:
      - name: move to urgent queue when 2 reviews and label urgent
        conditions:
          - base=main
          - "#approved-reviews-by>=2"
          - label=urgent
        actions:
          queue:
            name: urgent

      - name: merge using the merge queue
        conditions:
          - base=main
          - "#approved-reviews-by>=2"
          - check-success=Travis CI - Pull Request
          - label!=urgent
        actions:
          queue:
            name: default

It's also possible to move the pull request in the queue as soon as possible,
by passing the initial CI check. This allows the pull request to be considered
as a candidate even quicker. Even if the CI is not a condition to enter the
queue, in the following example, the ``urgent`` queue still ensures the CI
passes before merging the pull request.


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
          - base=main
          - "#approved-reviews-by>=2"
          - label=urgent
        actions:
          queue:
            name: urgent

      - name: move to default queue when CI passes and 2 reviews
        conditions:
          - base=main
          - "#approved-reviews-by>=2"
          - check-success=Travis CI - Pull Request
          - label!=urgent
        actions:
          queue:
            name: default

.. include:: ../global-substitutions.rst
