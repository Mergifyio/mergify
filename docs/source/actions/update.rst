.. meta::
   :description: Mergify Documentation for Update Action
   :keywords: mergify, update, merge, master, main, pull request
   :summary: Update a pull request with its base branch.
   :doc:icon: arrow-alt-circle-right

.. _update action:

update
======

The ``update`` action updates the pull request against its base branch. It
works by merging the base branch into the head branch of the pull request.

.. image:: ../_static/update-branch.png

.. code-block:: yaml

    actions:
      update:


Examples
--------

.. _example linear history:

↑ Linear History
~~~~~~~~~~~~~~~~~

As GitHub supports linear history in pull request settings, it is very handy to
use a rule to keep your pull requests up-to-date. As you do not want to trigger
your CI too often by always re-running it on every pull request — especially
when there is still work in progress — you can limit this action to labeled
pull requests.

.. code-block:: yaml

    pull_request_rules:
      - name: automatic update for PR marked as “Ready-to-Go“
        conditions:
          - -conflict # skip PRs with conflicts
          - -draft # filter-out GH draft PRs
          - label="Ready-to-Go"
        actions:
          update:

When a pull request is not in conflict nor draft, and has the label
``Ready-to-Go``, it will be automatically updated with its base branch.
