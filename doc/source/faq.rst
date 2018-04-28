===
FAQ
===

Where is the mergify policy file located ?
------------------------------------------

In the root directory of the repository with the name `.mergify.yaml`.

What the default policy configuration ?
---------------------------------------

.. code-block:: yaml

    required_status_checks:
      strict: True
      contexts:
          - continuous-integration/travis-ci
    required_pull_request_reviews:
      dismiss_stale_reviews: true
      require_code_owner_reviews: false
      required_approving_review_count: 1
    restrictions: null
    enforce_admins: true
