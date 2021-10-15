.. meta::
   :description: Mergify Documentation for Draft action
   :keywords: mergify, draft, pull request
   :summary: Convert pull requests to drafts. Useful to avoid pinging reviewers too early.
   :doc:icon: file-invoice

.. _draft action:

draft
=====

The ``draft`` action converts the pull request to a draft. It avoids
pinging reviewers too early and until the pull request is ready to be merged.

Examples
--------

📜 Converting a Pull Request to a Draft on Check Failure
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If any of your continuous integration checks fails, it might be worth converting
the pull request to a draft automatically since it's likely not ready to review.

.. code-block:: yaml

    pull_request_rules:
      - name: convert to draft
        conditions:
          - "#check-failure>0"
        actions:
          draft:
