Bots
~~~~

Some pull request might be created automatically by other tools, such as
`Dependabot <https://dependabot.com/>`_. You might decide that there's no need
to manually review and approve those pull request as long as your continuous
integration system validates them.

The rules below are samples for such services. ``Travis CI - Pull Request`` is
used as the CI, but it can be anything that you need.

Dependabot
----------

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for Dependabot pull requests
        conditions:
          - author=dependabot[bot]
          - status-success=Travis CI - Pull Request
        actions:
          merge:
            method: merge

