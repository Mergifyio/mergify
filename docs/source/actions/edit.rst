.. meta::
   :description: Mergify Documentation for Edit action
   :keywords: mergify, draft, edit, pull request
   :summary: Edit pull request attributes such as: draft state, body, title.
   :doc:icon: file-invoice

.. _edit action:

edit
=====

The ``edit`` action changes some specific attributes on the pull request.

Options
-------

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``draft``
     - Boolean
     - None
     - If the pull request should be a draft (``true``) or the other way around (``false``).


Examples
--------

📜 Converting a Pull Request to a Draft on Check Failure
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If any of your continuous integration checks fail, it might be worth converting
the pull request to a draft automatically since it's likely that it's not ready for review.

.. code-block:: yaml

    pull_request_rules:
      - name: convert to draft
        conditions:
          - "#check-failure>0"
        actions:
          edit:
            draft: True
