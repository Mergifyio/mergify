.. _Commands:

==========
 Commands
==========

Instead of waiting a list of :ref:`Conditions` to occur to run :ref:`Actions`,
Mergify bot can run the action when a special comment is sent on a pull
request.

The list of available commands is listed below, with their parameters:

.. _rebase command:

rebase
======

   Will run the :ref:`rebase action` action::

   @mergifyio rebase

.. _backport command:

backport
========

   Will run the :ref:`backport action` action::

   @mergifyio backport branch-1 branch-2
