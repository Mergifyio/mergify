.. meta::
   :description: Mergify Documentation for Assign Action
   :keywords: mergify, assign, pull request
   :summary: Assign or de-assign a pull request from a user.
   :doc:icon: hand-point-right

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
