.. meta::
   :description: Mergify Documentation for Backport Action
   :keywords: mergify, backport, copy, pull request
   :summary: Copy a pull request to another branch once it is merged.
   :doc:icon: reply

.. _backport action:

backport
=========

It is common for software to have (some of) their major versions maintained
over an extended period. Developers usually create stable branches that are
maintained for a while by cherry-picking patches from the development branch.

This process is called *backporting* as it implies that bug fixes merged into
the development branch are ported back to the stable branch(es). The stable
branch can then be used to release a new minor version of the software, fixing
some of its bugs.

As this process of backporting patches can be tedious, Mergify automates this
mechanism to save developers' time and ease their duty.

The ``backport`` action copies the pull request into another branch *once the
pull request has been merged*.

Once the backporting pull request is closed or merged, Mergify will
automatically delete the backport head branch that it created.

You can also trigger backports using the :ref:`backport command` command.

.. warning::

   |premium plan tag|
   |essential plan tag|
   If the repository is bigger than 512 MB, the ``backport`` action is only
   available for `Essential and Premium subscribers <https://mergify.io/pricing>`_.

Options
-------

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``branches``
     - list of string
     - ``[]``
     - The list of branches the pull request should be copied to.
   * - ``regexes``
     - list of string
     - ``[]``
     - The list of regexes to find branches the pull request should be copied
       to.
   * - ``ignore_conflicts``
     - Boolean
     - ``true``
     - Whether to create the pull requests even if they are conflicts when
       cherry-picking the commits.
   * - ``label_conflicts``
     - string
     - ``conflicts``
     - The label to add to the created pull request if it has conflicts and
       ``ignore_conflicts`` is set to ``true``.


Examples
--------

👩‍🔧 Using Labels to Backport Pull-Requests
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Copying pull requests to a maintenance branch is a common workflow. In order to
elect a pull request to be backported, it's possible to use a label. With a
rule such as the one below, you can trigger a backport as soon as the label is
added and the pull request merged:

.. code-block:: yaml

    pull_request_rules:
      - name: backport patches to stable branch
        conditions:
          - base=master
          - label=backport-to-stable
        actions:
          backport:
            branches:
              - stable

.. include:: ../global-substitutions.rst
