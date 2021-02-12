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
