.. meta::
   :description: Mergify Configuration Examples
   :keywords: mergify, examples

.. _examples:

=================
🧪 Example Rules
=================

Mergify allows you to define a lot of specific rules. There is a large number
of criterias available to define rules: pull request author, base branch,
labels, files, etc.

In this section, we build a few examples that should help you getting started
and cover many common use cases.

.. contents::
   :local:
   :depth: 1

✅ Automatic Merge when CI works and approving reviews
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This is a classic! That rule enables Mergify automatic merge when the
continuous integration system validates the pull request and a human reviewed
it.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for master when CI passes and 2 reviews
        conditions:
          - "#approved-reviews-by>=2"
          - check-success=Travis CI - Pull Request
          - base=master
        actions:
          merge:
            method: merge

You can tweak as fine as you want. For example, many users like to use a label
such as ``work-in-progress`` to indicate that a pull request is not ready to be
merged — even if's approved:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for master when CI passes and 2 reviews and not WIP
        conditions:
          - "#approved-reviews-by>=2"
          - check-success=Travis CI - Pull Request
          - base=master
          - label!=work-in-progress
        actions:
          merge:
            method: merge

You might want to merge a pull request only if it has been approved by a
certain member. You could therefore write such a rule:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for master when CI passes approved by octocat
        conditions:
          - approved-reviews-by=octocat
          - check-success=Travis CI - Pull Request
        actions:
          merge:
            method: merge

If you are already using the GitHub Branch Protection system. You can just
use:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge when GitHub branch protection passes on master
        conditions:
          - base=master
        actions:
          merge:
            method: merge


You can also remove the branch limitation so it'd work on any branch:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge when GitHub branch protection passes
        conditions: []
        actions:
          merge:
            method: merge


🌀 Using Regular Expressions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

You can use regular expressions in the :ref:`configuration file format`, associated with flags to enhance it.
To match a pull request title which contains the "WIP" statement, ignoring the case, you can write:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for master when the title contains “WIP” (ignoring case)
        conditions:
          - base=master
          - title~=(?i)wip
        actions:
          merge:
            method: merge

You can also use regular expressions to match filenames. For example, to merge your pull request
if at least one Python file is modified and if it passes Circle CI’s validation tests:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for master when CI passes and if at least one Python file is modified
        conditions:
          - "check-success=ci/circleci: validate"
          - files~=\.py$
        actions:
          merge:
            method: merge


🗂 Merging based on Modified Files
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

You could decide to only merge some pull requests based on the files they
touch. You can use the ``files`` attribute to access the modified file list and
``#files`` to access the number of files.

This tweak is useful when you want Mergify to merge only data files which can be
validated by the script, linter, etc.

The below sample merges only if ``data.json`` changed and if the pull request
passes Circle CI's validation tests:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge on CircleCI success if only data.json is changed
        conditions:
          - "check-success=ci/circleci: validate"
          - files=data.json
          - "#files=1"
        actions:
          merge:
            method: merge

You can also match patterns using regular expression. The following rule merges
the pull requests when the CI passes and when all the files are inside the
``src/`` directory:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge on CircleCI success if only data.json is changed
        conditions:
          - "check-success=ci/circleci: validate"
          - -files~=^(!?src/)
        actions:
          merge:
            method: merge


👩‍🔧 Using Labels to Backport Pull-Requests
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Copying pull requests to a maintenance  branch is something common as explained
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

You could also manually trigger them using the :ref:`backport command` command.

✂️ Deleting Merged Branch
~~~~~~~~~~~~~~~~~~~~~~~~~

Some users create pull request from the same repository by using different
branches — rather than creating a pull request from a fork. That's fine, but it
tends to leave a lot of useless branch behind when the pull request is merged.

Mergify allows to delete those branches once the pull request has been merged:

.. code-block:: yaml

    pull_request_rules:
      - name: delete head branch after merge
        conditions:
          - merged
        actions:
          delete_head_branch: {}


🏖 Less Strict Rules for Stable Branches
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Some projects like having easier review requirements for maintenance branches.
That usually means having e.g. 2 review requested for merging into ``master``,
but only one for a stable branch — since those pull request are essentially
backport from ``master``.

To automate the merge in this case, you could write some rules along those:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for master when reviewed and CI passes
        conditions:
          - "check-success=ci/circleci: my_testing_job"
          - "#approved-reviews-by>=2"
          - base=master
        actions:
          merge:
            method: merge
      - name: automatic merge for stable branches
        conditions:
          - "check-success=ci/circleci: my_testing_job"
          - "#approved-reviews-by>=1"
          - base~=^stable/
        actions:
          merge:
            method: merge


🎬 Using Labels to Enable/Disable Merge
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Some developers are not comfortable with fully automatic merge — they like
having a final word before merging the code. In that case, you can add a
condition using a `label
<https://help.github.com/articles/labeling-issues-and-pull-requests/>`_:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for master when reviewed and CI passes
        conditions:
          - check-success=Travis CI - Pull Request
          - "#approved-reviews-by>=2"
          - base=master
          - label=ready-to-merge
        actions:
          merge:
            method: merge

As soon as the pull request has been approved by 2 contributors and gets the
label ``ready-to-be-merged``, the pull request will be merged by Mergify.

On the other hand, some developers wants an option to disable the automatic
merge feature with a label. This can be useful to indicate that a pull request
labelled as ``work-in-progress`` should not be merged:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for master when reviewed and CI passes
        conditions:
          - check-success=continuous-integration/travis-ci/pr
          - "#approved-reviews-by>=2"
          - base=master
          - label!=work-in-progress
        actions:
          merge:
            method: merge

In that case, if a pull request gets labelled with ``work-in-progress``, it
won't be merged, even if approved by 2 contributors and having Travis CI
passing.

⚡️ Using Labels to Prioritize Merge
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

|premium plan tag|

When ``smart`` :ref:`strict merge` is enabled and many pull requests are
waiting to be merged, some of them might be more urgent. In that case, you
could add a condition using a `label
<https://help.github.com/articles/labeling-issues-and-pull-requests/>`_ and
configure the priority option of :ref:`merge action`:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge of 🚑 hotfix (high priority)
        conditions:
          - check-success=Travis CI - Pull Request
          - "#approved-reviews-by>=2"
          - base=master
          - label=🚑 hotfix
        actions:
          merge:
            method: merge
            strict: smart
            priority: high
      - name: automatic merge of bot 🤖 (low priority)
        conditions:
          - author~=^dependabot(|-preview)\[bot\]$
          - check-success=Travis CI - Pull Request
          - "#approved-reviews-by>=2"
          - base=master
        actions:
          merge:
            method: merge
            strict: smart
            priority: low
      - name: automatic merge for master when reviewed and CI passes
        conditions:
          - check-success=Travis CI - Pull Request
          - "#approved-reviews-by>=2"
          - base=master
        actions:
          merge:
            method: merge
            strict: smart
            priority: medium

As soon as the pull request has been approved by 2 contributors, the pull
request will be added to the merge queue. Within the merge queue, the pull
requests with the label ``🚑 hotfix`` will be merged first. The pull requests
from `dependabot` will always be merged last.

🥶 Removing Stale Reviews
~~~~~~~~~~~~~~~~~~~~~~~~~

When a pull request is updated, GitHub does not remove the (possibly) outdated
reviews approvals or changes request. It's a good idea to remove them as soon
as the pull request gets updated with new commits.

.. code-block:: yaml

    pull_request_rules:
      - name: remove outdated reviews
        conditions:
          - base=master
        actions:
          dismiss_reviews: {}

You could also only dismiss the outdated reviews if the author is not a member
of a particular team. This allows to keep the approval if the author is
trusted, even if they update their code:

.. code-block:: yaml

    pull_request_rules:
      - name: remove outdated reviews for non trusted authors
        conditions:
          - base=master
          - author!=@myorg/mytrustedteam
        actions:
          dismiss_reviews: {}

🙅️ Require All Requested Reviews to Be Approved
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If all requested reviews have been approved, then the number of
``review-requested``, ``changes-requested-reviews-by``, and
``commented-reviews-by`` should all be 0. You also want to make sure there's at
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

💌 Welcoming your Contributors
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When somebody that's not part of your team creates a pull requests, it might be
great to give him a few hints about what to expect next. You could write him a
little message.

.. code-block:: yaml

    pull_request_rules:
      - name: say hi on new contribution
        conditions:
          - author!=@myorg/regularcontributors
        actions:
            comment:
              message: |
                  Welcome to our great project!
                  We're delighted to have you onboard <3

🤜 Request for Action
~~~~~~~~~~~~~~~~~~~~~

If any event that requires the author of the pull request to edit its pull
request happen, you could write a rule that says something about it.

.. code-block:: yaml

    pull_request_rules:
      - name: ask to resolve conflict
        conditions:
          - conflict
        actions:
            comment:
              message: This pull request is now in conflicts. Could you fix it @{{author}}? 🙏

The same goes if one of your check fails. It might be good to give a few hints
to your contributor:

.. code-block:: yaml

    pull_request_rules:
      - name: ask to fix commit message
        conditions:
          - check-failure=Semantic Pull Request
          - -closed
        actions:
            comment:
              message: |
                Title does not follow the guidelines of [Conventional Commits](https://www.conventionalcommits.org).
                Please adjust title before merge.


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


💬 Running CI pipelines automatically
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Some continuous integration systems allow you to trigger jobs by commenting on
the pull request. For example, Microsoft Azure allows that using
the `/AzurePipelines command
<https://docs.microsoft.com/en-us/azure/devops/pipelines/repos/github?view=azure-devops&tabs=yaml#comment-triggers>`_.
You could automate this using Mergify, and for example trigger the job after
other CI jobs have passed and at least one developer has reviewed the pull
request.

.. code-block:: yaml

    pull_request_rules:
      - name: run Azure CI job once ready to merge
        conditions:
          - "#approved-reviews-by>=1"
          - "check-success=ci/circleci: my_testing_job"
          - -closed
        actions:
          comments:
            message: /AzurePipelines run mypipeline

📜 Enforcing Pull Request Guidelines
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

   |premium plan tag|

.. code-block:: yaml

    pull_request_rules:
      - name: Guidelines
        conditions:
          - "#title<50"
          - "#body<4096"
          - "#files<100"
          - "body~=(?m)^(Fixes|Related|Closes) (MERGIFY-ENGINE|MRGFY)-"
          - -label=ignore-guidelines
        actions:
          post_check:
            title: Pull request #{{ number }} does{% if not check_succeed %} not{% endif %} follow our guidelines
            summary: |
              My awesome pull request guidelines:

              * The tile must be at least 50 chars long
              * The body must be no longer than 4k chars
              * The PR can't modify more than 100 files
              * Please explain what you're trying to achieve by linking to an issue

              Conditions:

              {{ check_conditions }}


🐍 Updating PR for linear history
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

As GitHub supports linear history in pull request settings, it is very handy to use a rule
to keep your pull requests up-to-date. As you do not want to trigger your CI too often by always
re-running it on every pull request — especially when there is still work in progress — you can
limit this action to labeled pull requests.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic update for PR marked as “Ready-to-Go“
        conditions:
          - -conflict # skip PRs with conflicts
          - -draft # filter-out GH draft PRs
          - label="Ready-to-Go"
        actions:
          update: {}


.. include:: examples/bots.rst
.. include:: global-substitutions.rst
