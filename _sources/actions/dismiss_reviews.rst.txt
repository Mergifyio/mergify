.. meta::
   :description: Mergify Documentation for Dismiss Review Action
   :keywords: mergify, dismiss, review
   :summary: Dismiss previous reviews on a pull request.
   :doc:icon: user-slash

.. _dismiss_reviews action:

dismiss_reviews
===============

The ``dismiss_reviews`` action removes reviews done by collaborators when the
pull request is updated. This is especially useful to make sure that a review
does not stay when the branch is updated (e.g., new commits are added or the
branch is rebased).

Options
-------

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``approved``
     - Boolean or list of string
     - ``true``
     - If set to ``true``, all the approving reviews will be removed when the
       pull request is updated. If set to ``false``, nothing will be done. If
       set to a list, each item should be the GitHub login of a user whose
       review will be removed.
   * - ``changes_requested``
     - Boolean or list of string
     - ``true``
     - If set to ``true``, all the reviews requesting changes will be removed
       when the pull request is updated. If set to ``false``, nothing will be
       done. If set to a list, each item should be the GitHub login of a user
       whose review will be removed.
   * - ``message``
     - :ref:`data type template`
     - ``Pull request has been modified.``
     - The message to post when dismissing the review.

Examples
--------

🥶 Removing Stale Reviews
~~~~~~~~~~~~~~~~~~~~~~~~~

When a pull request is updated, GitHub does not remove the (possibly) outdated
reviews approvals or changes request. It's a good idea to remove them as soon
as the pull request gets updated with new commits.

.. code-block:: yaml

    pull_request_rules:
      - name: remove outdated reviews
        conditions:
          - base=main
        actions:
          dismiss_reviews:

You could also only dismiss the outdated reviews if the author is not a member
of a particular team. This allows to keep the approval if the author is
trusted, even if they update their code:

.. code-block:: yaml

    pull_request_rules:
      - name: remove outdated reviews for non trusted authors
        conditions:
          - base=main
          - author!=@mytrustedteam
        actions:
          dismiss_reviews:
