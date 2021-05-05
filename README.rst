mergify-engine
==============

.. image:: https://img.shields.io/endpoint.svg?url=https://gh.mergify.io/badges/Mergifyio/mergify.io
   :target: https://mergify.io
   :alt: Mergify Status

This is the engine running behind `Mergify <https://mergify.io>`_, a GitHub automation service for your pull requests.

This is how it works:

1. You write rules describing how to match a pull request, and which actions need to be executed.
2. The engine executes the action as soon as a pull request matches the conditions.

For example:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge when CI passes and 2 reviews
        conditions:
          - "#approved-reviews-by>=2"
          - status-success=Travis CI - Pull Request
          - base=main
        actions:
          merge:
            method: merge

That rule automatically merges a pull request targeting the `main` branch once it has 2 approving reviews and the CI passes.

You can learn more by browsing the `engine documentation <https://docs.mergify.io>`_.
