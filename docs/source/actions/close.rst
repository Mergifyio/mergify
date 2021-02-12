.. meta::
   :description: Mergify Documentation for Close Action
   :keywords: mergify, close, pull request
   :summary: Close pull requests.
   :doc:icon: times-circle

.. _close action:

close
=====

The ``close`` action closes the pull request without merging it.

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``message``
     - :ref:`data type template`
     - ``This pull request has been automatically closed by Mergify.``
     - The message to write as a comment after closing the pull request.
