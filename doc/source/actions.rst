.. _Actions:

=========
 Actions
=========

When a pull request matches the list of :ref:`Conditions` of a rule, the
actions set in the rule are executed by Mergify. The actions should be put
under the ``actions`` key in the ``pull_request_rules`` entry — see
:ref:`configuration file format`.

The list of available actions is listed below, with their parameters:

.. _assign action:

assign
======

The ``assign`` action assigns users to the pull request.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 2

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``users``
     - array of string
     - None
     - The users to assign to the pull request


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

.. _comment action:

comment
=======

The ``comment`` action adds a comment to the pull request.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 2

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``message``
     - string
     - None
     - The message to write as a comment.



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

This action takes no configuration options. The action will happen when the
pull request is closed or merged: you can decide what suits you best using
:ref:`Conditions`.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 2

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``force``
     - Boolean
     - ``False``
     - If set to ``True``, the branch will be deleted even if another pull
       request depends on the head branch. GitHub will therefore close the
       dependents pull requests.


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
       Possible values are ``merge``, ``squash``, ``null``.
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
   * - ``strict_method``
     - string
     - ``merge``
     - Base branch update method when strict mode is enabled.
       Possible values are ``merge`` or ``rebase``.

       Note that ``rebase`` has many drawbacks due to the change of all commits
       sha of the pull request. For example:

       * Your contributor will need to "force push" its own branch if it adds new commits.
       * GitHub branch protection of your repository may dismiss approved reviews.
       * GitHub branch protection of the contributor repository may refuse Mergify to
         force push the rebased pull request.
       * GPG signed commits will lost their signatures.
       * Also see: :ref:`faq strict rebase`

When a pull request is merged using the squash or merge method, Mergify uses
the default commit message provided by GitHub. You can override the commit
message by adding a section in the pull request body. The section must start
with the Markdown title "Commit Message" and contain the actual commit message.
For example::

    ## Commit Message
    My wanted commit title

    The whole commit message finishes at the end of the pull request body or
    before a new Markdown title.

.. _request_reviews action:

request_reviews
===============

The ``request_reviews`` action requests reviews from users for the pull request.

.. list-table::
  :header-rows: 1
  :widths: 1 1 1 2

  * - Key Name
    - Value Type
    - Default
    - Value Description
  * - ``users``
    - array of string
    - None
    - The users to request reviews from


Git merge workflow and Mergify equivalent configuration
-------------------------------------------------------

Examples without `strict: true` are obviously not recommended, more information
here: :ref:`strict merge`.

`base branch` is usually "master" or "dev",
`head branch` is the pull request branch.


.. list-table::
   :header-rows: 1
   :widths: 2 2

   * - Git merge workflow
     - Mergify configuration

   * - ::

         (on head branch) $ git merge --no-ff base

     - ::

         merge:
           method: merge

   * - ::

         (on head branch) $ git merge --no-ff base
         (on head branch) # Wait for CI to go green
         (on base branch) $ git merge --no-ff head

     - ::

         merge:
           strict: true
           method: merge

   * - ::

         (on head branch) $ git rebase base
         (on base branch) $ git merge --ff head

     - ::

         merge:
           method: rehead

   * - ::

         (on head branch) $ git merge --no-ff base
         (on head branch) # Wait for CI to go green
         (on head branch) $ git rebase base
         (on base branch) $ git merge --ff head

     - ::

         merge:
           strict: true
           method: rebase

   * - ::

         (on head branch) $ git rebase base
         (on head branch) # Wait for CI to go green
         (on base branch) $ git merge --no-ff head

     - ::

         merge:
           strict: true
           strict_method: rebase
           method: merge

   * - ::

        (on head branch) # Squash all commits
        (on base branch) $ git merge --ff head

     - ::

         merge:
           method: squash

   * - ::

         (on head branch) $ git merge --no-ff base
         (on head branch) # Wait for CI to go green
         (on head branch) # Squash all commits
         (on base branch) $ git merge --ff head

     - ::

         merge:
           strict: true
           method: squash

   * - ::

         (on head branch) $ git rebase base
         (on head branch) # Wait for CI to go green
         (on head branch) # Squash all commits
         (on base branch) $ git merge --ff head

     - ::

         merge:
           strict: true
           strict_method: rebase
           method: squash

   * - ::

         (on head branch) $ git rebase base
         (on head branch) # Squash all commits
         (on head branch) # Mergify wait for CI
         (on head branch) $ git merge --no-ff head

     - ::

         merge:
           strict: true
           strict_method: squash
           method: merge

       `(not yet implemented)`
