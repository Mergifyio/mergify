.. meta::
   :description: Mergify Documentation for Close Action
   :keywords: mergify, close, pull request
   :summary: Close pull requests.
   :doc:icon: times-circle

.. _close action:

close
=====

The ``close`` action closes the pull request — without merging it.

Options
-------

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

Examples
--------

❌ Automatically close pull request touching a file
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you wanted to automatically close pull request that touches a certain file,
let's say ``do_not_touch.txt``, you could write a rule such as:

.. code-block:: yaml

    pull_request_rules:
      - name: disallow changing a file
        conditions:
          - files=do_not_touch.txt
        actions:
          close:
