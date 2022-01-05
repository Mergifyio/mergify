.. meta::
   :description: Mergify Documentation for Assign Action
   :keywords: mergify, subscription, pull request
   :summary: Subscribe users to pull requests.
   :doc:icon: hand-point-right

.. _subscription action:

subscription
============

The ``subscription`` action subscribe users to the pull request.

Options
-------

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``subscribed``
     - list of :ref:`data type template`
     -
     - The users to notify of all conversations.
   * - ``unsubscribed``
     - list of :ref:`data type template`
     -
     - The users to notify only when participating or @mentioned.


The list of users in ``subscribed`` or ``unsubscribed`` is based
on :ref:`data type template`, you can use e.g. ``{{author}}`` to subscribe its
author.

