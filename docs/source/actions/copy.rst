.. meta::
   :description: Mergify Documentation for Copy Action
   :keywords: mergify, copy, pull request
   :summary: Copy a pull request and create a new pull request.
   :doc:icon: copy

.. _copy action:

copy
====

The ``copy`` action creates a copy of the pull request targetting other branches.

.. warning::

   |premium plan tag|
   |essential plan tag|
   If the repository is bigger than 512 MB, the ``copy`` action is only
   available for `Essential and Premium  Plan subscribers <https://mergify.io/pricing>`_.

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
   * - ``label_conflicts``
     - string
     - ``conflicts``
     - The label to add to the created pull request if it has conflicts and
       ``ignore_conflicts`` is set to ``true``.

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
