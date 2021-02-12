.. meta::
   :description: Mergify Documentation for Merge Action
   :keywords: mergify, merge, pull request
   :summary: Merge a pull request.
   :doc:icon: code-branch

.. _merge action:

=======
 merge
=======

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

.. include:: ../global-substitutions.rst
