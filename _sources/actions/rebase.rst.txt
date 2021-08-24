.. meta::
   :description: Mergify Documentation for Rebase Action
   :keywords: mergify, rebase, pull request
   :summary: Rebase a pull request on top of its base branch.
   :doc:icon: undo

.. _rebase action:

rebase
======


The ``rebase`` action will rebase the pull request against its base branch. To
this effect, it clones the branch, run `git rebase` locally and push back the
result to the GitHub repository.

.. warning::

   |premium plan tag|
   |essential plan tag|
   If the repository is bigger than 512 MB, the ``rebase`` action is only
   available for `Essential and Premium Plan subscribers
   <https://mergify.io/pricing>`_.

Options
-------

.. list-table::
  :header-rows: 1
  :widths: 1 1 1 2

  * - Key Name
    - Value Type
    - Default
    - Value Description

  * - ``bot_account``
    - :ref:`data type template`
    -
    - |premium plan tag|
      For certain actions, such as rebasing branches, Mergify has to
      impersonate a GitHub user. You can specify the account to use with this
      option. If no ``bot_account`` is set, Mergify picks randomly one of the
      organization users instead. The user account **must** have already been
      logged in Mergify dashboard once.

.. include:: ../global-substitutions.rst
