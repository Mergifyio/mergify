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

`Dependabot <https://github.com/features/security>`_ is the bot behind GitHub
automatic security update. It sends automatic updates for your project's
dependencies, making sure they are secure.

You can automate the merge of pull request created by `dependabot` with a rule
such as:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for Dependabot pull requests
        conditions:
          - author~=^dependabot(|-preview)\[bot\]$
          - check-success=Travis CI - Pull Request
        actions:
          merge:
            method: merge

Alternatively, you can also enable the automatic merge for `dependabot` pull
request only if they are for the same major version.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for Dependabot pull requests
        conditions:
          - author~=^dependabot(|-preview)\[bot\]$
          - check-success=Travis CI - Pull Request
          - title~=^Bump [^\s]+ from ([\d]+)\..+ to \1\.
        actions:
          merge:
            method: merge

The additional `title` based rule in ``conditions`` make sure that only the
updates from `1.x` to `1.y` will be automatically merged. Updates from, e.g.,
`2.x` to `3.x` will be ignored by Mergify.

Snyk
----
`Snyk <https://snyk.io>`_ sends automatic updates for your
project's dependencies.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for Snyk pull requests
        conditions:
          - title~=^\[Snyk\]
          - head~=^snyk-fix
          - check-success~=^security/snyk
        actions:
          merge:
            method: merge

Renovate
--------
`Renovate <https://renovatebot.com/>`_ sends automatic updates for your
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

Requires.io
-----------
`Requires.io <https://requires.io/>`_ sends automatic updates for your
project's dependencies (only Python's packages).

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge for Requires.io pull requests
        conditions:
          - title~=^\[requires.io\]
          - head~=^requires-io
          - check-success=Travis CI - Pull Request
        actions:
          merge:
            method: merge

PyUp
----
`PyUp <https://pyup.io/>`_ sends automatic updates for your project's
Python dependencies.

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
