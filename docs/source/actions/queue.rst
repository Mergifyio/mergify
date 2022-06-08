.. meta::
   :description: Mergify Documentation for Queue Action
   :keywords: mergify, queue, pull request
   :summary: Put a pull request in queue before merging.
   :doc:icon: train

.. _queue action:

queue
=====

The ``queue`` action moves the pull request into one of the merge queue defined
in :ref:`queue rules`.

Merge queues prevent merging broken pull requests by serializing their merge.
Merging broken pull requests can happen when outdated pull requests are being
merged in their base branch.

Mergify always respects the `branch protection`_ settings. When the conditions
match and the ``queue`` action runs, Mergify waits for the branch protection to
be validated before embarking and merging the pull request.

.. _`branch protection`: https://docs.github.com/en/github/administering-a-repository/about-protected-branches

Mergify also waits for dependent pull requests to get merged first (see :ref:`merge-depends-on`).


Why Queues?
-----------

.. _merge queue problem:

To understand the problem queues resolve, imagine the following situation:

- The base branch (e.g., ``main``) has its continuous integration testing
  passing correctly.

- A pull request is created, which also passes the CI.

The state of the repository can be represented like this:

.. graphviz:: strict-mode-master-pr-ci-pass.dot
   :alt: Pull request is open

While the pull request is open, another commit is pushed to ``main`` — let's
call it `new commit`. That new commit can be pushed directly to ``main`` or
merged from another pull request; it doesn't matter.

The tests are run against the ``main`` branch by the CI, and they pass. The
state of the repository and its continuous integration system can be now
described like this:

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
did not test the pull request with the `new commit` added to the base branch.
This `new commit` might have introduced some new tests in the base branch while
the pull request was open. That pull request may not have the correct code to
pass this new test.

Using Queues
~~~~~~~~~~~~

Using a merge queue solves that issue by updating any pull request that is not
up-to-date with its base branch before being merged. That forces the continuous
integration system to retest the pull request with the new code from its base
branch.

If a merge queue were being used in the previous example, Mergify would
automatically merge the ``main`` in the base branch. The continuous integration
system would have rerun and marked the pull request as failing the test,
removing it from the merge queue altogether.

.. graphviz:: strict-mode-rebase-ci-fail.dot
   :alt: Rebase make CI fails

When multiple pull requests are mergeable, they are scheduled to be merged
sequentially, and are updated on top of each other. The pull request branch
update is only done when the pull request is ready to be merged by the engine,
e.g., when all the `conditions` are validated.

That means that when a first pull request has been merged, and the second one
is outdated like this:

.. graphviz:: queue-strict-merge-pr2-open.dot
   :alt: strict merge with PR2 open

Mergify will make sure the pull request #2 is updated with the latest tip of
the base branch before merging:

.. graphviz:: queue-strict-merge.dot
   :alt: strict merge

That way, there's no way to merge a broken pull request into the base branch.

Configuring the Merge Queues
----------------------------

The `merge queues` rely on two configuration items: the ``queues_rules`` and
the ``queue`` action.

The ``queue`` action places the pull request inside the merge queue: the set of
conditions described in the ``pull_request_rules`` allows the pull request to
enter the queue when all conditions match.

If the ``queue`` action conditions do not match anymore, the pull request is
removed from the merge queue.

When a pull request is at the top of the queue, Mergify looks at the second set
of conditions defined this time in ``queue_rules`` and wait that they all match
to merge the pull request.

When multiple queues are defined, they are processed one after the other in the
order they are defined in the ``queue_rules`` list. Queue processing is blocked
until all preceding queues are empty.

Viewing the Merge Queue
-----------------------

The merge queue can be visualized from your `dashboard <https://dashboard.mergify.com>`_:

.. figure:: ../_static/merge-queue.png
   :alt: The merge queue from the dashboard

.. _speculative checks:

Speculative Checks
------------------

|premium plan tag|

Merging pull requests one by one serially can take a lot of time, depending on
the continuous integration run time. To merge pull requests faster, Mergify
queues support `speculative checks`.

With speculative checks, the first pull requests from the queue are embarked in
a `merge train` and tested together in parallel so they can be merged faster. A
merge train consists of two or more pull requests embarked together to be
tested speculatively. To test them, Mergify creates temporary pull requests
where multiple queued pull requests are merged together.

The upside of creating multiple temporary pull requests is that continuous
integration pipelines can run on all of them in parallel. Currently, Mergify
embarks up from 1 to 20 pull requests into the merge train (you can set the
value with the ``speculative_checks`` settings in ``queue_rules``).

.. graphviz:: queue-strict-train.dot
   :alt: Merge train
   :caption: A merge queue with 5 pull requests and ``speculative_checks`` set to 3

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

.. warning::

   With speculative checks, pull requests are tested against the base branch
   within another temporary pull request. This requires the branch protection
   settings ``Require branches to be up to date before merging`` to be
   disabled. If you require a linear history, just set the queue option
   ``method: rebase``.

Batch Size
----------

|premium plan tag|

Mergify allows checking the mergeability of multiple pull requests at once using
the ``batch_size`` option. If set to ``3``, Mergify will create a draft pull
request with the latest version of the base branch and merge the commits of the 3
next queued pull requests.

If the CI validates the draft pull request, Mergify will merge the 3 queued
pull requests and close the draft one. If the CI reports a failure, Mergify uses a binary
search to build a smaller batch of pull requests to check in order to find the culprit.
Once the failing pull request is found, Mergify removes it from the queue. The rest of the
queue is processed as usual.

``batch_size`` and ``speculative_checks`` can be combined to increase the
merge queue throughput.

For example, if your queue is 15 pull requests long and you have
``speculative_checks`` set to ``3`` and ``batch_size`` to ``5``, you'll have 3
draft pull requests of 5 pull requests each being tested at the same time. If
your CI time is 10 min, you can merge those 15 pull requests in only 10 minutes.

.. warning::

   With batch mode, pull requests are tested against the base branch within
   another temporary pull request. This requires the branch protection settings
   ``Require branches to be up to date before merging`` to be disabled. If you
   require a linear history, just set the queue option ``method: rebase``.

Queue Freeze
------------

|premium plan tag|

Mergify allows freezing the merge of one or several queues simultaneously to provide maximum control
and flexibility on how and when you want the code to be merged.

The freeze feature is available through `the Mergify API <https://docs.mergify.com/api>`_.
A set of API endpoints allows you to create, get, update and delete freezes on your queues.

When a queue is frozen, all the checks and CI configured on the queue rules are still scheduled and triggered;
only the merge is blocked. Freezing allows you to continue your work, development, and testing while ensuring
that no pull requests will be merged from the queue.

For example, let's say you have three queues, respectively ``hotfix``, ``default`` and ``lowprio``, in this order.

You are in the middle of an incident for your project, and want your production branch
not to receive anything other than bug fixes. By creating a freeze on the ``default`` queue, you are now
assured that nothing from this queue  — nor the queue after it, ``lowprio`` — will be merged.

Meanwhile, you can still merge pull requests pushed to the ``hotfix`` queue.

Once your incident is resolved, you can unfreeze the queue ``default`` and returns to the previous state.

🚫 Unqueued Pull Request
------------------------

Mergify removes a pull request from the queue if:

* a user has manually unqueued the pull request;
* there are CI failures;
* there is a CI timeout.

To get the pull request back in the queue, you can either:

* retrigger the CI if the problem was due to a CI failure (such as a flaky test) or a timeout;
* update the code inside the pull request, which will retrigger the CI
* use the :ref:`requeue command` to inform Mergify that the CI failure was not due to the pull request itself, but to a, e.g., a flaky test.

.. _merge-depends-on:

⛓️ Defining Pull Request Dependencies
-------------------------------------

|premium plan tag|
|open source plan tag|

You can specify dependencies between pull requests from the same repository.
Mergify waits for the linked pull requests to be merged before merging any pull
request with a ``Depends-On:`` header.

To use this feature, adds the ``Depends-On:`` header to the body of your pull
request:

.. code-block:: md

    New awesome feature 🎉

    To get the full picture, you may need to look at these pull requests:

    Depends-On: #42
    Depends-On: https://github.com/organization/repository/pull/123

.. warning::

    This feature does not work for cross-repository dependencies.

.. _update method rebase:

Using Rebase to Update
----------------------

Using the ``rebase`` method to update a pull request with its base branch has some
drawbacks:

* It doesn't work for private forked repositories.

* Every commits SHA-1 of the pull request will change. The pull request author
  will need to force-push its own branch if they add new commits.

* GitHub branch protections of your repository may dismiss approved reviews.

* GitHub branch protection of the contributor repository may deny Mergify
  force-pushing the rebased pull request.

* GPG signed commits will lose their signatures.

* Mergify will impersonate one of the repository members to force-push the
  branch as GitHub Applications are not authorized to do that by themselves.
  If you need to control which user is impersonated, you can use the ``update_bot_account`` option.
  Be aware that the GitHub UI will show the collaborator as the author of the push, while it was actually executed by Mergify.

Options
-------

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
     - Merge method to use. Possible values are ``merge``, ``squash``,
       ``rebase`` or ``fast-forward``.
       ``fast-forward`` is not supported on queues with ``speculative_checks > 1``,
       ``batch_size > 1``, or with ``allow_inplace_checks`` set to ``false``.
   * - ``rebase_fallback``
     - string
     - ``merge``
     - If ``method`` is set to ``rebase``, but the pull request cannot be
       rebased, the method defined in ``rebase_fallback`` will be used instead.
       Possible values are ``merge``, ``squash``, ``none``. ``none`` will
       report an error if rebase is not possible.

   * - ``merge_bot_account``
     - :ref:`data type template`
     -
     - |premium plan tag|
       Mergify can impersonate a GitHub user to merge pull request.
       If no ``merge_bot_account`` is set, Mergify will merge the pull request
       itself. The user account **must** have already been
       logged in Mergify dashboard once and have **write** or **maintain** permission.

   * - ``update_method``
     - string
     - ``merge`` for all merge methods except ``fast-forward`` where ``rebase`` is used
     - Method to use to update the pull request with its base branch when the
       speculative check is done in-place.
       Possible values:

       * ``merge`` to merge the base branch into the pull request.
       * ``rebase`` to rebase the pull request against its base branch.

       Note that the ``rebase`` method has some drawbacks, see :ref:`update method rebase`.
   * - ``update_bot_account``
     - :ref:`data type template`
     -
     - |premium plan tag|
       For certain actions, such as rebasing branches, Mergify has to
       impersonate a GitHub user. You can specify the account to use with this
       option. If no ``update_bot_account`` is set, Mergify picks randomly one of the
       organization users instead. The user account **must** have already been
       logged in Mergify dashboard once.

   * - ``priority``
     - 1 <= integer <= 10000 or ``low`` or ``medium`` or ``high``
     - ``medium``
     - |premium plan tag| This sets the priority of the pull request in the queue. The pull
       request with the highest priority is merged first.
       ``low``, ``medium``, ``high`` are aliases for ``1000``, ``2000``, ``3000``.

   * - ``commit_message_template``
     - :ref:`data type template`
     -
     - Template to use as the commit message when using the ``merge`` or ``squash`` merge method.
       Template can also be defined in the pull request body (see :ref:`commit message`).

   * - ``require_branch_protection``
     - bool
     - true
     - Whether branch protections are required for queueing pull requests. This
       option is ignored if the target queue has ``speculative_checks > 1``.

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
     - list of :ref:`Conditions`
     -
     - The list of ``conditions`` to match to get the queued pull request merged,
       In case of speculative merge pull request, the conditions starting by
       ``check-`` are evaluated against the temporary pull request instead of the
       original one.
   * - ``speculative_checks``
     - int
     - 1
     - |premium plan tag| The maximum number of checks to run in parallel in the queue. Must be
       between 1 and 20.
       See :ref:`speculative checks`.
   * - ``allow_inplace_checks``
     - bool
     - True
     - Allow to update/rebase the original pull request if possible to check
       its mergeability when ``speculative_checks: 1`` and ``batch_size: 1``.
       If ``False``, a draft pull request is always created.
   * - ``disallow_checks_interruption_from_queues``
     - list of ``queue`` names
     -
     - |premium plan tag| The list of higher priorities ``queue`` that are not
       allowed to interrupt the ongoing checks of this queue.
   * - ``allow_checks_interruption``
     - bool
     - True
     - Allow interrupting the ongoing speculative checks when a pull request
       with higher priority enters in the queue.
       If ``False``, pull request with higher priority will be inserted just after the pull requests that
       have checks running.

   * - ``batch_size``
     - int
     - 1
     - |premium plan tag| The maximum number of pull requests per speculative check in the queue. Must be
       between 1 and 20.
       See :ref:`speculative checks`.

   * - ``batch_max_wait_time``
     - :ref:`Duration <duration>`
     - 30 s
     - |premium plan tag| The time to wait before creating the speculative check temporary pull request.
       See :ref:`speculative checks`.

   * - ``checks_timeout``
     - :ref:`Duration <duration>`
     -
     - The amount of time Mergify waits for pending checks to return before unqueueing pull requests.
       This cannot be less than 60 seconds.

   * - ``draft_bot_account``
     - string
     -
     - |premium plan tag|
       Mergify can impersonate a GitHub user to create draft pull requests.
       If no ``draft_bot_account`` is set, Mergify creates the draft pull request
       itself. The user account **must** have already been
       logged in Mergify dashboard once and have **admin**, **write** or
       **maintain** permission.


.. note::

   |premium plan tag|
   Defining multiple queue rules is only available for `Premium subscribers <https://mergify.com/pricing>`_.


Examples
--------

🐛 Single Queue
~~~~~~~~~~~~~~~

A simple usage of the queue is to merge pull requests serially, ensuring they
all pass the CI one after the other. The following example defines a queue
named ``default`` which allows any pull request to be merged once it enters it.

To enter the queue, pull requests need to be based on the ``main`` branch, have
2 approving reviews and pass the CI. Once a pull request is in first position
in the queue, it will be updated with the latest commit of its base branch.

.. code-block:: yaml

    queue_rules:
      - name: default
        conditions: []  # no extra conditions needed to get merged

    pull_request_rules:
      - name: merge using the merge queue
        conditions:
          - base=main
          - "#approved-reviews-by>=2"
          - check-success=Travis CI - Pull Request
        actions:
          queue:
            name: default


🚥 Multiple Queues
~~~~~~~~~~~~~~~~~~
|premium plan tag|

By using multiple queues, it's possible to put some pull requests in a higher
priority queue. As queues are processed one after the other, the following
example allow to add a label ``urgent`` to a pull request so it gets put in the
higher priority queue.

.. code-block:: yaml

    queue_rules:
      - name: urgent
        conditions:
          # A PR can be queued without the CI passing, but needs to get merged though
          - check-success=Travis CI - Pull Request

      - name: default
        conditions: []

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

With such a configuration, a pull request with the label ``urgent`` will get
into the queue as soon as it's approved by 2 developers but before the CI has
even run on it. It will be in front of the ``default`` queue. Mergify will
update the pull request with its base branch if necessary, wait for the CI to
pass and then merge the pull request.


🎲 Speculative Checks
~~~~~~~~~~~~~~~~~~~~~
|premium plan tag|

If your continuous integration system takes a long time to validate the
enqueued pull requests, it might be interesting to enable :ref:`speculative
checks <speculative checks>`. This will allow Mergify to trigger multiple runs
of the CI in parallel.

In the following example, by setting the ``speculative_checks`` option to
``2``, Mergify will create up to 2 new pull requests to check if the first
three enqueued pull requests are mergeable.

.. code-block:: yaml

    queue_rules:
      - name: default
        speculative_checks: 2
        conditions: []

    pull_request_rules:
      - name: merge using the merge queue and speculative checks
        conditions:
          - base=main
          - "#approved-reviews-by>=2"
          - check-success=Travis CI - Pull Request
        actions:
          queue:
            name: default

Multiple pull requests can be checked within one speculative check by settings
``batch_size``.

For example, by settings ``speculative_checks: 2`` and ``batch_size: 3``,
Mergify will create two pull requests: a first one to check if the first three
enqueued pull requests are mergeable, and a second one to check the three next
enqueued pull requests.

.. code-block:: yaml

    queue_rules:
      - name: default
        speculative_checks: 2
        batch_size: 2
        conditions: []

    pull_request_rules:
      - name: merge using the merge queue and speculative checks
        conditions:
          - base=main
          - "#approved-reviews-by>=2"
          - check-success=Travis CI - Pull Request
        actions:
          queue:
            name: default

.. _unqueue_notification:

🤙 Get notified when a pull request is unexpectedly unqueued
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: yaml

    - name: Notify author on queue failure
      conditions:
        - 'check-failure=Queue: Embarked in merge train'
      actions:
        comment:
          message: >
            Hey @{{ author }}, this pull request failed to merge and has been
            dequeued from the merge train.  If you believe your PR failed in
            the merge train because of a flaky test, requeue it by commenting
            with `@mergifyio requeue`.
            More details can be found on the `Queue: Embarked in merge train`
            check-run.


To receive ``mentions`` on Slack, you can then use the `native GitHub
integration <https://github.com/integrations/slack#scheduled-reminder>`_.

.. include:: ../global-substitutions.rst
