.. meta::
   :description: Mergify Documentation for Review Action
   :keywords: mergify, review, pull request
   :summary: Review a pull request.
   :doc:icon: thumbs-up

.. _review action:

review
=======

The ``review`` action reviews the pull request.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``type``
     - string
     - ``APPROVE``
     - The kind of review, can be ``APPROVE``, ``REQUEST_CHANGES``, ``COMMENT``
   * - ``message``
     - :ref:`data type template`
     -
     - The message to write as a comment.
   * - ``bot_account``
     - :ref:`data type template`
     -
     - Mergify can impersonate a GitHub user to review a pull request.
       If no ``bot_account`` is set, Mergify will review the pull request
       itself.

       |premium plan tag|

.. include:: ../global-substitutions.rst
