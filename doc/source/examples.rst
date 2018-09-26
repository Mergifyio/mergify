.. _examples:

===============
 Example Rules
===============

You can define more specific rules based on the large number of criterias
available: pull request author, base branch, labels, files, etc.

Here's a few example that should help you getting started.

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
          - status-success=continuous-integration/travis-ci
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
          - status-success=continuous-integration/travis-ci
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


Automatic Merge for Automatic Pull Requests
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Some pull request might be created automatically by other tools, such as
`Dependabot <https://dependabot.com/>`_. You might decide that there's no need
to manually review and approve those pull request as long as your continuous
integration system validates them.

Therefore, you could write a rule such as:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for Dependabot pull requests on master
        conditions:
          - author=dependabot[bot]
          - status-success=continuous-integration/travis-ci
          - base=master
        actions:
          merge:
            method: merge

That would automatically merge any pull request created by Dependabot for the
``master`` branch where Travis CI passes.

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
          - status-success=continuous-integration/travis-ci
          - "#approved-reviews-by>=2"
          - base=master
        actions:
          merge:
            method: merge
      - name: automatic merge for stable branches
        conditions:
          - status-success=continuous-integration/travis-ci
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
          - status-success=continuous-integration/travis-ci
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
          - status-success=continuous-integration/travis-ci
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
          dismiss_reviews:
