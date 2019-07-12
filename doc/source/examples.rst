.. _examples:

===============
 Example Rules
===============

You can define more specific rules based on the large number of criterias
available: pull request author, base branch, labels, files, etc.

Here's a few example that should help you getting started.

.. contents::
   :local:
   :depth: 2

Automatic Merge when CI works and approving reviews
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This is the classic rule that enables Mergify automatic merge when the
continuous integration system validates the pull request and a human reviewed
it.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge when CI passes and 2 reviews
        conditions:
          - "#approved-reviews-by>=2"
          - status-success=Travis CI - Pull Request
          - base=master
        actions:
          merge:
            method: merge

You can tweak as fine as you want. For example, many users like to use a label
such as ``work-in-progress`` to indicate that a pull request is not ready to be
merged — even if's approved.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge when CI passes and 2 reviews
        conditions:
          - "#approved-reviews-by>=2"
          - status-success=Travis CI - Pull Request
          - base=master
          - label!=work-in-progress
        actions:
          merge:
            method: merge


Using Labels to Backport Pull-Requests
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Copying pull requests to a maintenance branch is something common as explained
in :ref:`backport action`. In order to elect a pull request to be backported,
it's common to use a label. You could write a rule such as:

.. code-block:: yaml

    pull_request_rules:
      - name: backport patches to stable branch
        conditions:
          - base=master
          - label=backport-to-stable
        actions:
          backport:
            branches:
              - stable


Deleting Merged Branch
~~~~~~~~~~~~~~~~~~~~~~

Some users create pull request from the same repository by using different
branch, rather than creating a pull request from a fork. That's fine, but it
tends to leave a lot of useless branch behind when the pull request is merged.

Mergify allows to delete those branches once the pull request has been merged:

.. code-block:: yaml

    pull_request_rules:
      - name: delete head branch after merge
        conditions: []
        actions:
          delete_head_branch: {}


Less Strict Rules for Stable Branches
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Some projects like having easier review requirements for stable/maintenance
branches. That usually means having e.g. 2 review requested for merging into
master, but only one for a stable branch, since those pull request are
essentially backport from ``master``.

To automate the merge in this case, you could write some rules along those:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for master when reviewed and CI passes
        conditions:
          - "status-success=ci/circleci: my_testing_job"
          - "#approved-reviews-by>=2"
          - base=master
        actions:
          merge:
            method: merge
      - name: automatic merge for stable branches
        conditions:
          - "status-success=ci/circleci: my_testing_job"
          - "#approved-reviews-by>=1"
          - base~=^stable/
        actions:
          merge:
            method: merge


Using Labels to Enable/Disable Merge
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Some developers are not comfortable with having a final step before merging the
code. In that case, you can add a condition using a ``label``:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for master when reviewed and CI passes
        conditions:
          - status-success=Travis CI - Pull Request
          - "#approved-reviews-by>=2"
          - base=master
          - label=ready-to-merge
        actions:
          merge:
            method: merge

As soon as the pull request has been approved by 2 contributors and gets the
`label <https://help.github.com/articles/labeling-issues-and-pull-requests/>`_
``ready-to-be-merged``, the pull request will be merged by Mergify.

On the other hand, some developers wants an option to disable the automatic
merge feature with a label. This can be useful to indicate that a pull request
labelled as ``work-in-progress`` should not be merged:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for master when reviewed and CI passes
        conditions:
          - status-success=continuous-integration/travis-ci/pr
          - "#approved-reviews-by>=2"
          - base=master
          - label!=work-in-progress
        actions:
          merge:
            method: merge

In that case, if a pull request gets labelled with ``work-in-progress``, it
won't be merged, even if approved by 2 contributors and having Travis CI
passing.

Removing Stale Reviews
~~~~~~~~~~~~~~~~~~~~~~

When a pull request is updated, GitHub does not remove the possibly outdated
reviews approvals or changes request. It's a good idea to remove them as soon
as the pull request gets updated with new commits.

.. code-block:: yaml

    pull_request_rules:
      - name: remove outdated reviews
        conditions:
          - base=master
        actions:
          dismiss_reviews: {}

Require All Requested Reviews to Be Approved
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If all requested reviews have been approved, then the number of
``review-requested``, ``changes-requested-reviews-by``, and
``commented-reviews-by`` will all be 0. You also want to make sure there's at
least one positive review, obviously.

.. code-block:: yaml

    pull_request_rules:
      - name: merge when all requested reviews are valid
        conditions:
          - "#approved-reviews-by>=1"
          - "#review-requested=0"
          - "#changes-requested-reviews-by=0"
          - "#commented-reviews-by=0"
        actions:
            merge:
              method: merge

Note that if a requested review is dismissed, then it doesn't count as a review
that would prevent the merge.

Using Files to Narrow Down the Files to Merge
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

After the CI pass, to automate the merge only if the specified files are changed
and no other files, then use ``files`` to specify the file and ``#files`` to
limit the number of files.

This tweak is useful when you want Mergify to merge only data files which can be
validated by the script, linter, etc.

The below sample merges only if both or either one of ``data1.json`` and
``data2.json`` file is changed and passes Circle CI's validation tests:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge on CircleCI success if data1.json is changed
        conditions:
          - "status-success=ci/circleci: validate"
          - base=master
          - files=data1.json
          - "#files=1"
        actions:
          merge:
            method: merge
            strict: true

      - name: automatic merge on CircleCI success if list2.json is changed
        conditions:
          - "status-success=ci/circleci: validate"
          - base=master
          - files=data2.json
          - "#files=1"
        actions:
          merge:
            method: merge
            strict: true


.. include:: examples/bots.rst
