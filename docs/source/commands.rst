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

.. warning::

   If the repository is bigger than 512 MB, the ``rebase`` action is only
   available for `Essential and Premium Plan subscribers <https://mergify.io/pricing>`_.
   |essential plan tag|
   |premium plan tag|

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

.. warning::

   If the repository is bigger than 512 MB, the ``backport`` action is only
   available for `Essential and Premium Plan subscribers <https://mergify.io/pricing>`_.
   |essential plan tag|
   |premium plan tag|

refresh
========

Re-evalutes your Mergify rules on this pull request.

.. list-table::
  :widths: 1 7
  :align: left

  * - Syntax
    - ``@Mergifyio refresh``
  * - Example
    - ``@Mergifyio refresh``

.. include:: global-substitutions.rst
