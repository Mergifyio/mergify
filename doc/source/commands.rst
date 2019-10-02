.. _Commands:

==========
 Commands
==========

:ref:`Actions` can be run directly by commenting on a pull request. The syntax is::

  @Mergifyio [parameters …]

The list of available commands is listed below, with their parameters:

.. _rebase command:

rebase
======

   Will run the :ref:`rebase action` action.

.. list-table::
  :widths: 1 7
  :align: left

  * - Syntax
    - ``@Mergifyio rebase``
  * - Example
    - ``@Mergifyio rebase``

.. _backport command:

backport
========

   Will run the :ref:`backport action` action.

.. list-table::
  :widths: 1 7
  :align: left

  * - Syntax
    - ``@Mergifyio backport <branch name> <branch name 2> …``
  * - Example
    - ``@Mergifyio backport stable/3.1 stable/4.0``
