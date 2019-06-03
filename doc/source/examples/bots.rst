Bots
~~~~

Some pull request might be created automatically by other tools, such as the
ones sending automatic dependencies update. You might decide that there's no
need to manually review and approve those pull request as long as your
continuous integration system validates them.

The rules below are samples for such services: they are designed to
automatically merged those updates without human interview if the continuous
integration system validates them. ``Travis CI - Pull Request`` is used as the
CI check name here, but it can be anything that you need.

Dependabot
----------
`Dependabot <https://dependabot.io>`_ sends automatic security update of your
project dependencies.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for Dependabot pull requests
        conditions:
          - author=dependabot[bot]
          - status-success=Travis CI - Pull Request
        actions:
          merge:
            method: merge


Greenkeeper
-----------
`Greenkeeper <https://greenkeeper.io>`_ sends automatic update of your `npm`
dependencies.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for Greenkeeper pull requests
        conditions:
          - author=greenkeeper[bot]
          - status-success=Travis CI - Pull Request
          - status-sucess=greenkeeper/verify
        actions:
          merge:
            method: merge

Renovate
--------
`Renovate <https://renovatebot.com/>`_ sends automatic update for your
project's dependencies.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for Renovate pull requests
        conditions:
          - author=renovate[bot]
          - status-success=Travis CI - Pull Request
        actions:
          merge:
            method: merge

PyUp
----
`PyUp <https://pyup.io/>`_ sends automatic update for your project's Python
dependencies.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for PyUp pull requests
        conditions:
          - author=pyup-bot
          - status-success=Travis CI - Pull Request
        actions:
          merge:
            method: merge
