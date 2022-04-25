.. meta::
   :description: Mergify Documentation for Merge Action
   :keywords: mergify, merge, pull request
   :summary: Merge a pull request.
   :doc:icon: code-branch

.. _merge action:

=======
 merge
=======

The ``merge`` action merges the pull request into its base branch.

Mergify always respects the `branch protection`_ settings. When the conditions
match and the ``merge`` action runs, Mergify waits for the branch protection to
be validated before merging the pull request.

.. _`branch protection`: https://docs.github.com/en/github/administering-a-repository/about-protected-branches

Mergify also waits for dependent pull requests to get merged first (see :ref:`queue-depends-on`).

Options
-------

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
     - Merge method to use. Possible values are ``merge``, ``squash``,
       ``rebase`` or ``fast-forward``.
   * - ``rebase_fallback``
     - string
     - ``merge``
     - If ``method`` is set to ``rebase``, but the pull request cannot be
       rebased, the method defined in ``rebase_fallback`` will be used instead.
       Possible values are ``merge``, ``squash``, ``none``. ``none`` will
       report an error if rebase is not possible.
   * - ``merge_bot_account``
     - :ref:`data type template`
     -
     - |premium plan tag|
       Mergify can impersonate a GitHub user to merge pull request.
       If no ``merge_bot_account`` is set, Mergify will merge the pull request
       itself. The user account **must** have already been
       logged in Mergify dashboard once and have **write** or **maintain** permission.

       .. _commit_message_template:
   * - ``commit_message_template``
     - :ref:`data type template`
     -
     - Template to use as the commit message when using the ``merge`` or ``squash`` merge method.
       Template can also be defined in the pull request body (see :ref:`commit message`).

.. _queue-depends-on:

⛓️ Defining Pull Request Dependencies
-------------------------------------

|premium plan tag|
|open source plan tag|

You can specify dependencies between pull requests from the same repository.
Mergify waits for the linked pull requests to be merged before merging any pull
request with a ``Depends-On:`` header.

To use this feature, adds the ``Depends-On:`` header to the body of your pull
request:

.. code-block:: md

    New awesome feature 🎉

    To get the full picture, you may need to look at these pull requests:

    Depends-On: #42
    Depends-On: https://github.com/organization/repository/pull/123

.. warning::

    This feature does not work for cross-repository dependencies.

.. warning::

    If the dependency happens between pull requests targeting different
    branches, the evaluation of the dependent will not be automatic. You might
    need to use the :ref:`refresh command <refresh command>` to make Mergify
    realize the dependency has been merged.


.. _commit message:

Defining the Commit Message
---------------------------

When a pull request is merged using the ``squash`` or ``merge`` method, you can
override the default commit message. To that end, you need to set
:ref:`commit_message_template <commit_message_template>`.

You can use part of the pull request body in the ``Commit Message`` Markdown section,
by setting it to:

.. code-block:: jinja

    {{ body | get_section("## Commit Message") }}

Then in the pull request body you can use:

.. code-block:: md

    ## Commit Message

    My wanted commit title

    The whole commit message finishes at the end of the pull request body or
    before a new Markdown title.

The whole commit message finishes at the end of the Markdown section.

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


Or you can also mix template from the configuration and the pull request body
by setting :ref:`commit_message_template <commit_message_template>` to:

.. code-block:: jinja

    {{ body | get_section("## Commit Message") }}

    {% for user in approved_reviews_by %}
    - {{user}}
    {% endfor %}

    {% for label in labels %}
    - {{label}}
    {% endfor %}


.. include:: ../global-substitutions.rst
