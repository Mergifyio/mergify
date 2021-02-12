.. meta::
   :description: Mergify Documentation for Comment Action
   :keywords: mergify, Comment, pull request
   :summary: Post a comment on a pull request.
   :doc:icon: comment

.. _comment action:

comment
=======

The ``comment`` action adds a comment to the pull request.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``message``
     - :ref:`data type template`
     -
     - The message to write as a comment.
   * - ``bot_account``
     - :ref:`data type template`
     -
     - Mergify can impersonate a GitHub user to comment a pull request.
       If no ``bot_account`` is set, Mergify will comment the pull request
       itself.

       |premium plan tag|

.. include:: ../global-substitutions.rst
