.. meta::
   :description: Mergify Documentation for Actions
   :keywords: mergify, automation, actions

.. _Actions:

==========
🚀 Actions
==========

When a pull request matches the list of :ref:`Conditions` of a rule, the
actions configured in that rule are executed by Mergify. The actions should be
put under the ``actions`` key in the ``pull_request_rules`` entry — see
:ref:`configuration file format`.

The list of available actions is listed below, with their parameters:

.. _assign action:

assign
======

The ``assign`` action assigns users to the pull request.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``add_users``
     - list of :ref:`data type template`
     -
     - The users to assign to the pull request.
   * - ``remove_users``
     - list of :ref:`data type template`
     -
     - The users to remove from assignees.

The list of users in ``add_users`` or ``remove_users`` is based on :ref:`data type template`, you can use
e.g. ``{{author}}`` to assign the pull request to its author.

.. _backport action:

backport
=========

It is common for software to have (some of) their major versions maintained
over an extended period. Developers usually create stable branches that are
maintained for a while by cherry-picking patches from the development branch.

This process is called *backporting* as it implies that bug fixes merged into
the development branch are ported back to the stable branch(es). The stable
branch can then be used to release a new minor version of the software, fixing
some of its bugs.

As this process of backporting patches can be tedious, Mergify automates this
mechanism to save developers' time and ease their duty.

The ``backport`` action copies the pull request into another branch *once the
pull request has been merged*. The ``backport`` action takes the following
parameter:

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``branches``
     - list of string
     - ``[]``
     - The list of branches the pull request should be copied to.
   * - ``regexes``
     - list of string
     - ``[]``
     - The list of regexes to find branches the pull request should be copied
       to.
   * - ``ignore_conflicts``
     - Boolean
     - ``true``
     - Whether to create the pull requests even if they are conflicts when
       cherry-picking the commits.
   * - ``label_conflicts``
     - string
     - ``conflicts``
     - The label to add to the created pull requested if it has conflicts and
       ``ignore_conflicts`` is set to ``true``.


Once the backporting pull request is closed or merged, Mergify will
automatically delete the backport head branch that it created.

.. warning::

   If the repository is bigger than 512 MB, the ``backport`` action is only
   available for `Essential and Premium subscribers <https://mergify.io/pricing>`_.
   |essential plan tag|
   |premium plan tag|

copy
====

The ``copy`` action creates a copy of the pull request targetting other branches.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``branches``
     - list of string
     - ``[]``
     - The list of branches the pull request should be copied to.
   * - ``regexes``
     - list of string
     - ``[]``
     - The list of regexes to find branches the pull request should be copied to.
   * - ``ignore_conflicts``
     - Boolean
     - ``true``
     - Whether to create the pull requests even if they are conflicts when
       cherry-picking the commits.
   * - ``label_conflicts``
     - string
     - ``conflicts``
     - The label to add to the created pull requested if it has conflicts and
       ``ignore_conflicts`` is set to ``true``.


.. warning::

   If the repository is bigger than 512 MB, the ``copy`` action is only
   available for `Essential and Premium  Plan subscribers <https://mergify.io/pricing>`_.
   |essential plan tag|
   |premium plan tag|

.. _comment action:

comment
=======

The ``comment`` action adds a comment to the pull request.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``message``
     - :ref:`data type template`
     -
     - The message to write as a comment.
   * - ``bot_account``
     - :ref:`data type template`
     -
     - Mergify can impersonate a GitHub user to comment a pull request.
       If no ``bot_account`` is set, Mergify will comment the pull request
       itself.

       |premium plan tag|

.. _review action:

review
=======

The ``review`` action reviews the pull request.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``type``
     - string
     - ``APPROVE``
     - The kind of review, can be ``APPROVE``, ``REQUEST_CHANGES``, ``COMMENT``
   * - ``message``
     - :ref:`data type template`
     -
     - The message to write as a comment.
   * - ``bot_account``
     - :ref:`data type template`
     -
     - Mergify can impersonate a GitHub user to review a pull request.
       If no ``bot_account`` is set, Mergify will review the pull request
       itself.

       |premium plan tag|

.. _close action:

close
=====

The ``close`` action closes the pull request without merging it.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``message``
     - :ref:`data type template`
     - ``This pull request has been automatically closed by Mergify.``
     - The message to write as a comment after closing the pull request.

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


.. _label action:

label
=====

The ``label`` action can add or remove `labels
<https://help.github.com/articles/about-labels/>`_ from a pull request.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``add``
     - list of string
     - ``[]``
     - The list of labels to add.
   * - ``remove``
     - list of string
     - ``[]``
     - The list of labels to remove.
   * - ``remove_all``
     - Boolean
     - ``false``
     - Remove all labels from the pull request.

.. _merge action:

merge
=====

The ``merge`` action merges the pull request into its base branch. The
``merge`` action takes the following parameter:

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

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
     - Boolean, ``smart`` or ``smart+fasttrack``
     - ``false``
     - Determines whether to use :ref:`strict merge`:

       * ``true`` enables :ref:`strict merge`. The pull request will be merged
         only once up-to-date with its base branch. When multiple pull requests
         are ready to be merged, they will **all** be updated with their base
         branch at the same time, and the first ready to be merged will be
         merged; the remaining pull request will be updated once again.

       * ``smart`` enables :ref:`strict merge` but only update one pull request
         against its base branch at a time.
         This allows you to e.g., save CI time, as Mergify will queue the
         mergeable pull requests and update them serially, one at a time.

       * ``smart+fasttrack`` enables :ref:`strict merge` with the same behavior as ``smart``,
         except if the pull request is already in sync with its base branch,
         the queue is bypassed and the pull request is directly merged.

       * ``false`` disables :ref:`strict merge` and merge pull requests as soon
         as possible, without bringing the pull request up-to-date with its
         base branch.


   * - ``strict_method``
     - string
     - ``merge``
     - Method to use to update the pull request with its base branch
       when :ref:`strict merge` is enabled. Possible values:

       * ``merge`` to merge the base branch into the pull request.
       * ``rebase`` to rebase the pull request against its base branch.

       Note that the ``rebase`` method has some drawbacks, see :ref:`strict
       rebase`.

   * - ``merge_bot_account``
     - :ref:`data type template`
     -
     - Mergify can impersonate a GitHub user to merge pull request.
       If no ``merge_bot_account`` is set, Mergify will merge the pull request
       itself. The user account **must** have already been
       logged in Mergify dashboard once and have **write** or **maintain** permission.

       |premium plan tag|

   * - ``update_bot_account``
     - :ref:`data type template`
     -
     - For certain actions, such as rebasing branches, Mergify has to
       impersonate a GitHub user. You can specify the account to use with this
       option. If no ``update_bot_account`` is set, Mergify picks randomly one of the
       organization users instead. The user account **must** have already been
       logged in Mergify dashboard once.

       |premium plan tag|

   * - ``priority``
     - 1 <= integer <= 10000 or ``low`` or ``medium`` or ``high``
     - ``medium``
     - This sets the priority of the pull request in the queue when ``smart``
       :ref:`strict merge` is enabled. The pull request with the highest priority is merged first.
       ``low``, ``medium``, ``high`` are aliases for ``1000``, ``2000``, ``3000``.

       |premium plan tag|

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

Branch Protection Settings
--------------------------

Note that Mergify will always respect the branch protection settings. When the
conditions match and the ``merge`` action runs, Mergify waits for the branch
protection to be validated before merging the pull request.

.. _commit message:

Defining the Commit Message
---------------------------

When a pull request is merged using the ``squash`` or ``merge`` method, you can
override the default commit message. To that end, you need to add a section in
the pull request body that starts with ``Commit Message``.

.. code-block:: md

    ## Commit Message

    My wanted commit title

    The whole commit message finishes at the end of the pull request body or
    before a new Markdown title.

The whole commit message finishes at the end of the pull request body or before
a new Markdown title.

You can use any available attributes of the pull request in the commit message,
by writing using the :ref:`templating <data type template>` language:

For example:

.. code-block:: jinja

    ## Commit Message

    {{title}}

    This pull request implements magnificient features, and I would like to
    talk about them. This has been written by {{author}} and has been reviewed
    by:

    {% for user in approved_reviews_by %}
    - {{user}}
    {% endfor %}

Check the :ref:`data type template` for more details on the format.

.. note::

   This feature only works when ``commit_message`` is set to ``default``.

.. _strict rebase:

Strict Rebase
-------------

Using the ``rebase`` method for the strict merge has many drawbacks:

* It doesn't work for private forked repositories.

* It doesn't work if Mergify is used as a GitHub Action.

* Due to the change of all commits SHA-1 of the pull request, your
  contributor will need to force-push its own branch if they add new
  commits.

* GitHub branch protection of your repository may dismiss approved reviews.

* GitHub branch protection of the contributor repository may refuse Mergify to
  force push the rebased pull request.

* GPG signed commits will lost their signatures.

* Mergify will use a token from one of the repository member to force-push the
  branch. GitHub Applications are not allowed to do that so. This is a GitHub
  limitation that we already have reported — there is not much Mergify can do
  about that. In order to make this works, Mergify randomly picks and borrows a
  token from one of your repository member and uses it to force-push the
  rebased branch. The GitHub UI will show your collaborator as the author of
  the push, while it actually has been executed by Mergify.

.. _request_reviews action:

queue
=====

|premium plan tag|
|beta tag|

The ``queue`` action moves the pull request into one of the merge queue defined
in ``queue_rules``. The ``queue`` action takes the following parameter:

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
   * - ``merge_bot_account``
     - string
     -
     - Mergify can impersonate a GitHub user to merge pull request.
       If no ``merge_bot_account`` is set, Mergify will merge the pull request
       itself. The user account **must** have already been
       logged in Mergify dashboard once and have **write** or **maintain** permission.
   * - ``priority``
     - 1 <= integer <= 10000 or ``low`` or ``medium`` or ``high``
     - ``medium``
     - This sets the priority of the pull request in the queued. The pull
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


request_reviews
===============

The ``request_reviews`` action requests reviews from users for the pull
request.

.. list-table::
  :header-rows: 1
  :widths: 1 1 1 2

  * - Key Name
    - Value Type
    - Default
    - Value Description
  * - ``users``
    - list of string or dictionary of login and weight
    -
    - The username to request reviews from.
  * - ``teams``
    - list of string or dictionary of login and weight
    -
    - The team name to request reviews from.
  * - ``random_count``
    - integer between 1 and 15
    -
    - Pick random users and teams from the provided lists. When
      ``random_count`` is specified, ``users`` and ``teams`` can be a
      dictionary where the key is the login and the value is the weight to use.
      Weight must be between 1 and 65535 included.

      |essential plan tag|

.. note::

   GitHub does not allow to request more 15 users or teams for a review.

.. _rebase action:

rebase
======

The ``rebase`` action will rebase the pull request against its base branch.

.. list-table::
  :header-rows: 1
  :widths: 1 1 1 2

  * - Key Name
    - Value Type
    - Default
    - Value Description

  * - ``bot_account``
    - :ref:`data type template`
    -
    - For certain actions, such as rebasing branches, Mergify has to
      impersonate a GitHub user. You can specify the account to use with this
      option. If no ``bot_account`` is set, Mergify picks randomly one of the
      organization users instead. The user account **must** have already been
      logged in Mergify dashboard once.

.. warning::

   Be aware that rebasing force-pushes the pull request head branch: any change
   done to that branch while Mergify is rebasing will be lost.

.. warning::

   If the repository is bigger than 512 MB, the ``rebase`` action is only
   available for `Essential and Premium  Plan subscribers <https://mergify.io/pricing>`_.
   |essential plan tag|
   |premium plan tag|

.. _update action:

update
======

The ``update`` action updates the pull request against its base branch.

.. code-block:: yaml

    actions:
      update: {}


post_check
==========

|premium plan tag|

The ``post_check`` action adds an item in a pull request check list. The check
status is ``success`` when all conditions match, otherwise, it is set to
``failure``.


.. list-table::
  :header-rows: 1
  :widths: 1 1 1 2

  * - Key Name
    - Value Type
    - Default
    - Value Description

  * - ``title``
    - :ref:`data type template`
    -
    - The title of the check.

  * - ``summary``
    - :ref:`data type template`
    -
    - The summary of the check.


As the ``title`` and ``summary`` use on :ref:`data type template`, you can
benefit from any pull request attributes e.g. ``{{author}}`` and also these
additional variables:

    * ``{{ check_rule_name }}`` the name of the rule that triggered this action.
    * ``{{ check_succeed }}`` is ``True`` if all conditions matches otherwise ``False``
    * ``{{ check_conditions }}`` the list of all conditions with a checkbox marked if the condition match


.. include:: global-substitutions.rst
