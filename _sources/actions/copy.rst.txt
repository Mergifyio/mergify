.. meta::
   :description: Mergify Documentation for Copy Action
   :keywords: mergify, copy, pull request
   :summary: Copy a pull request and create a new pull request.
   :doc:icon: copy

.. _copy action:

copy
====

.. warning::

   |premium plan tag|
   |essential plan tag|
   If the repository is bigger than 512 MB, the ``copy`` action is only
   available for `Essential and Premium  Plan subscribers <https://mergify.io/pricing>`_.

The ``copy`` action creates a copy of the pull request targeting other branches.

Options
-------

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
   * - ``bot_account``
     - :ref:`data type template`
     -
     - |premium plan tag|
       Mergify can impersonate a GitHub user to copy a pull request.
       If no ``bot_account`` is set, Mergify copies the pull request
       itself.
   * - ``labels``
     - list of string
     - ``[]``
     - The list of labels to add to the created pull requests.
   * - ``label_conflicts``
     - string
     - ``conflicts``
     - The label to add to the created pull request if it has conflicts and
       ``ignore_conflicts`` is set to ``true``.
   * - ``assignees``
     - list of :ref:`data type template`
     -
     - Users to assign the newly created pull request. As the type is
       :ref:`data type template`, you could use, e.g., ``{{author}}`` to assign
       the pull request to its original author.
   * - ``title``
     - :ref:`data type template`
     - ``{{ title }} (copy #{{ number }})``
     - The pull request title.
   * - ``body``
     - :ref:`data type template`
     - ``This is an automatic copy of pull request #{{number}} done by [Mergify](https://mergify.io).\n{{cherry_pick_error}}``
     - The pull request body.


As the ``title`` and ``body`` are :ref:`templates <data type template>`, you can
leverage any pull request attributes to use as content, e.g. ``{{author}}``.
You can also use this additional variable:

    * ``{{ destination_branch }}``: the name of destination branch.
    * ``{{ cherry_pick_error }}``: the cherry pick error message if any (only available in ``body``).

Examples
--------

🔁 Copy a Pull Request to Another Branch
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The following rule copies a pull request from the ``staging`` branch to the
``prod`` branch as soon as the CI passes and a label ``ready for prod`` is set
on the pull request:

.. code-block:: yaml

    pull_request_rules:
      - name: copy pull request when CI passes
        conditions:
          - base=staging
          - check-success=test
          - label=ready for prod
        actions:
          copy:
            branches:
              - prod

.. include:: ../global-substitutions.rst
