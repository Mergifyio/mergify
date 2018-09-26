.. _Actions:

=========
 Actions
=========

When a pull request matches the list of :ref:`Conditions` of a rule, the
actions set in the rule are executed by Mergify. The actions should be put
under the ``actions`` key in the ``pull_request_rules`` entry — see
:ref:`configuration file format`.

The list of available actions is listed below, with their parameters:

.. _backport action:

backport
=========

It is common for software to have (some of) their major versions maintained
over an extended period. Developers usually create stable branches that are
maintained for a while by cherry-picking patches from the development branch.

This process is called *backporting* as it implies that bug fixes merged into
the development branch are ported back to the stable branch. The stable branch
can then be used to release a new minor version of the software, fixing some of
its bugs.

As this process of backporting patches can be tedious, Mergify automates this
mechanism to save developers' time and ease their duty.

The ``backport`` action copies the pull request into another branch *once the
pull request has been merged*. The ``backport`` action takes the following
parameter:

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 2

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``branches``
     - array of string
     - ``[]``
     - The list of branches the pull request should be copied to.

Once the backporting pull request is closed or merged, Mergify will
automatically delete the backport head branch that it created.

.. _close action:

close
=====

The ``close`` action closes the pull request without merging it.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 2

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``message``
     - string
     - ``This pull request has been automatically closed by Mergify.``
     - The message to write as a comment after closing the pull request.


.. _delete_head_branch action:

delete_head_branch
==================

The ``delete_head_branch`` action deletes the head branch of the pull request,
that is the branch which hosts the commits. This only works if the branch is
stored in the same repository that the pull request target, i.e., if the pull
request comes from the same repository and not form a fork.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 2

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``on``
     - ``merge`` or ``close``
     - ``merge``
     - When to delete the head branch: ``merge`` will only delete the branch
       when the pull request is merged, whereas ``close`` will delete the
       branch if the pull request is merged or closed.

.. _dismiss_reviews action:

dismiss_reviews
===============

The ``dismiss_reviews`` action removes reviews done by collaborators when the
pull request is updated. This is especially useful to make sure that a review
does not stay when the branch is updated (e.g., new commits are added or the
branch is rebeased).

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 2

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``approved``
     - Boolean or array of string
     - ``True``
     - If set to ``True``, all the approving reviews will be removed when the
       pull request is updated. If set to ``False``, nothing will be done. If
       set to a list, each item should be the GitHub login of a user whose
       review will be removed.
   * - ``changes_requested``
     - Boolean or array of string
     - ``True``
     - If set to ``True``, all the reviews requesting changes will be removed
       when the pull request is updated. If set to ``False``, nothing will be
       done. If set to a list, each item should be the GitHub login of a user
       whose review will be removed.

.. _label action:

label
=====

The ``label`` action can add or remove `labels
<https://help.github.com/articles/about-labels/>`_ from a pull request.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 2

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``add``
     - array of string
     - ``[]``
     - The list of labels to add.
   * - ``remove``
     - array of string
     - ``[]``
     - The list of labels to remove.

.. _merge action:

merge
=====

The ``merge`` action merges the pull request into its base branch. The
``merge`` action takes the following parameter:

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 2

   * - Key Name
     - Value Type
     - Default
     - Value Description
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
       Possible values are ``merge``, ``squash``, ``none``.
   * - ``strict``
     - Boolean or ``smart``
     - ``false``
     - If set to ``true``, :ref:`strict merge` will be enabled: the pull
       request will be merged only once up-to-date with its base branch. When
       multiple pull requests are ready to be merged, they will all be updated
       with their base branch at the same time, and the first ready to be
       merged will be merged; the remaining pull request will be updated once
       again. If you prefer to update one pull request at a time (for example,
       to save CI runtime), set ``strict`` to ``smart`` instead: Mergify will
       queue the mergeable pull requests and update them one at a time serially.
