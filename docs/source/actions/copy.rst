.. meta::
   :description: Mergify Documentation for Copy Action
   :keywords: mergify, copy, pull request
   :summary: Copy a pull request and create a new pull request.
   :doc:icon: copy

.. _copy action:

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

.. include:: ../global-substitutions.rst
