.. meta::
   :description: Mergify Configuration Examples for Bots
   :keywords: mergify, examples, dependabot, greenkeeper, renovate, pyup, imgbot

🤖 Bots
~~~~~~~

Some pull requests might be created automatically by other tools, such as the
ones sending automatic dependencies update. You might decide that there's no
need to manually review and approve those pull request as long as your
continuous integration system validates them.

The rules below are examples for such services: they are designed to
automatically merge those updates without human intervention if the continuous
integration system validates them. ``Travis CI - Pull Request`` is used as the
CI check name here — adjust it to whatever you use.

Dependabot
----------
`Dependabot <https://dependabot.io>`_ sends automatic security update of your
project dependencies.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for Dependabot pull requests
        conditions:
          - author~=^dependabot(|-preview)\[bot\]$
          - check-success=Travis CI - Pull Request
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
          - check-success=Travis CI - Pull Request
          - check-success=greenkeeper/verify
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
          - check-success=Travis CI - Pull Request
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
          - check-success=Travis CI - Pull Request
        actions:
          merge:
            method: merge

ImgBot
------
`ImgBot <https://github.com/marketplace/imgbot>`_ optimizes your images and
saves you time.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for ImgBot pull requests
        conditions:
          - author=imgbot[bot]
          - check-success=Travis CI - Pull Request
        actions:
          merge:
            method: merge
