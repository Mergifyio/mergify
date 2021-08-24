.. meta::
   :description: Mergify Documentation for Review Action
   :keywords: mergify, review, pull request
   :summary: Review a pull request.
   :doc:icon: thumbs-up

.. _review action:

review
=======

The ``review`` action reviews the pull request. You can use it to approve or
request a change on a pull request.

Options
-------

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``type``
     - string
     - ``APPROVE``
     - The kind of review, can be ``APPROVE``, ``REQUEST_CHANGES``, ``COMMENT``
   * - ``message``
     - :ref:`data type template`
     -
     - The message to write as a comment.
   * - ``bot_account``
     - :ref:`data type template`
     -
     - |premium plan tag| Mergify can impersonate a GitHub user to review a pull request.
       If no ``bot_account`` is set, Mergify will review the pull request
       itself.

Examples
--------

You can use Mergify to review a pull request on your behalf. This can be handy
if you require a minimum number of review to be present on a pull request
(e.g., due to `branch protection`_) but wants to automate some merge.

`Dependabot <https://github.com/features/security>`_ is the bot behind GitHub
automatic security update. It sends automatic updates for your project's
dependencies, making sure they are secure. You can automate the approval of
pull request created by `dependabot` with a rule such as:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic approval for Dependabot pull requests
        conditions:
          - author~=^dependabot(|-preview)\[bot\]$
        actions:
          review:
            type: APPROVE
            message: Automatically approving dependabot


.. _`branch protection`: https://docs.github.com/en/github/administering-a-repository/about-protected-branches

.. include:: ../global-substitutions.rst
