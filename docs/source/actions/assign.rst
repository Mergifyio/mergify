.. meta::
   :description: Mergify Documentation for Assign Action
   :keywords: mergify, assign, pull request
   :summary: Assign or de-assign a pull request from a user.
   :doc:icon: hand-point-right

.. _assign action:

assign
======

The ``assign`` action assigns users to the pull request.

Options
-------

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


Examples
--------

👉 Flexible Assignment
~~~~~~~~~~~~~~~~~~~~~~

You can assign people for review based on any criteria you like. A classic is
to use the name of modified files to do it:

.. code-block:: yaml

    pull_request_rules:
      - name: assign PRs with Python files modified to jd
        conditions:
          - files~=\.py$
          - -closed
        actions:
          assign:
            add_users:
              - jd
