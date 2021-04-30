.. meta::
   :description: Mergify Documentation for Squash Action
   :keywords: mergify, squash, pull request
   :summary: Squash a pull request.
   :doc:icon: compress-arrows-alt

.. _squash action:

squash
=======

The ``squash`` action transforms pull request's n-commits into a single commit.


Options
-------

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``bot_account``
     - :ref:`data type template`
     -
     - |premium plan tag|
       Mergify can impersonate a GitHub user to squash a pull request.
       If no ``bot_account`` is set, Mergify will squash the pull request
       itself.

   * - ``commit_message``
     - string
     - ``all-commits``
     - Defines what commit message to use for the squashed commit if not commit
       message is defined in the pull request body (see :ref:`commit message`).
       Possible values are:

       * ``all-commits`` to use the same format as GitHub squashed merge commit.

       * ``first-commit`` to use the message of the first commit of the pull request.

       * ``title+body`` means to use the title and body from the pull request
         itself as the commit message. The pull request number will be added to
         end of the title.


.. include:: ../global-substitutions.rst
