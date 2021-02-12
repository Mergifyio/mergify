.. meta::
   :description: Mergify Documentation for Delete Head Branch Action
   :keywords: mergify, delete, head branch, branch, pull request
   :summary: Delete pull request head branch. Useful to clean pull request once closed.
   :doc:icon: trash

.. _delete_head_branch action:

delete_head_branch
==================

The ``delete_head_branch`` action deletes the head branch of the pull request,
that is the branch which hosts the commits. This only works if the branch is
stored in the same repository that the pull request target, i.e., if the pull
request comes from the same repository and not from a fork.

.. note::

   The action will only happen if and when the pull request is closed or merged.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``force``
     - Boolean
     - ``false``
     - If set to ``true``, the branch will be deleted even if another pull
       request depends on the head branch. GitHub will therefore close the
       dependent pull requests.
