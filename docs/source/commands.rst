.. meta::
   :description: Mergify Documentation for Commands
   :keywords: mergify, commands

.. _Commands:

============
 ⚙️ Commands
============

Mergify is able to run some :ref:`Actions` directly without leveraging the rule
system. By commenting on a pull request, you can ask Mergify to executes some
actions.

The syntax of the comment is::

  @Mergifyio <command> [parameters …]

.. note::

  To run commands, a user needs to be the author of the pull request or have write access to the repository.

The list of available commands is listed below, with their parameters:

.. _rebase command:

rebase
======

Runs the :ref:`rebase action` action.

.. list-table::
  :widths: 1 7
  :align: left

  * - Syntax
    - ``@Mergifyio rebase``
  * - Example
    - ``@Mergifyio rebase``

.. _update command:

update
======

Runs the :ref:`update action` action.

.. list-table::
  :widths: 1 7
  :align: left

  * - Syntax
    - ``@Mergifyio update``
  * - Example
    - ``@Mergifyio update``

.. _backport command:

backport
========

Runs the :ref:`backport action` action.

.. list-table::
  :widths: 1 7
  :align: left

  * - Syntax
    - ``@Mergifyio backport <branch name> <branch name 2> …``
  * - Example
    - ``@Mergifyio backport stable/3.1 stable/4.0``

.. _copy command:

copy
====

Runs the :ref:`copy action` action.

.. list-table::
  :widths: 1 7
  :align: left

  * - Syntax
    - ``@Mergifyio copy <branch name> <branch name 2> …``
  * - Example
    - ``@Mergifyio copy stable/3.1 stable/4.0``

refresh
========

Re-evaluates your Mergify rules on this pull request.

.. list-table::
  :widths: 1 7
  :align: left

  * - Syntax
    - ``@Mergifyio refresh``
  * - Example
    - ``@Mergifyio refresh``

.. include:: global-substitutions.rst
