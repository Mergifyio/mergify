.. meta::
   :description: Mergify Documentation for Edit action
   :keywords: mergify, draft, edit, pull request
   :summary: Edit pull request attributes such as: draft state, body, title.
   :doc:icon: file-invoice

.. _edit action:

edit
=====

The ``edit`` action changes some specific attributes on the pull request.
The ``draft`` parameter converts the pull request to a draft or the other way
around according to a boolean defined. The draft state avoids pinging 
reviewers too early and until the pull request is ready to be merged.

Examples
--------

📜 Converting a Pull Request to a Draft on Check Failure
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If any of your continuous integration checks fails, it might be worth converting
the pull request to a draft automatically since it's likely that it's not ready for review.

.. code-block:: yaml

    pull_request_rules:
      - name: convert to draft
        conditions:
          - "#check-failure>0"
        actions:
          edit:
            draft: True