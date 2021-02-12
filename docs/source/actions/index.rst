.. meta::
   :description: Mergify Documentation for Actions
   :keywords: mergify, automation, actions

.. _Actions:

==========
🚀 Actions
==========

When a pull request matches the list of :ref:`Conditions` of a rule, the
actions configured in that rule are executed by Mergify. The actions should be
put under the ``actions`` key in the ``pull_request_rules`` entry — see
:ref:`configuration file format`.

The list of available actions is listed below, with their configuration
options.

.. raw:: html
   :file: action-list.html

.. toctree::
   :maxdepth: 1

   assign
   backport
   close
   copy
   comment
   delete_head_branch
   dismiss_reviews
   label
   merge
   post_check
   queue
   rebase
   request_reviews
   review
   update
