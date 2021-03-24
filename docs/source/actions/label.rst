.. meta::
   :description: Mergify Documentation for Label Action
   :keywords: mergify, label, pull request
   :summary: Add or remove a label from a pull request.
   :doc:icon: tag

.. _label action:

label
=====

The ``label`` action can add or remove `labels
<https://help.github.com/articles/about-labels/>`_ from a pull request.

Options
-------

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``add``
     - list of string
     - ``[]``
     - The list of labels to add.
   * - ``remove``
     - list of string
     - ``[]``
     - The list of labels to remove.
   * - ``remove_all``
     - Boolean
     - ``false``
     - Remove all labels from the pull request.

Examples
--------

💥 Warn on Conflicts
~~~~~~~~~~~~~~~~~~~~

When browsing the list of pull request, GitHub does not give any indication on
which pull requests might be in conflict. Mergify allows to do this easily by
adding a label.


.. code-block:: yaml

    pull_request_rules:
      - name: warn on conflicts
        conditions:
          - conflict
        actions:
          comment:
            message: "@{{author}} this pull request is now in conflict 😩"
          label:
            add:
              - conflict
      - name: remove conflict label if not needed
        conditions:
          - -conflict
        actions:
          label:
            remove:
              - conflict

Then, you pull request list will look like this on conflict:

.. image:: ../_static/conflict-pr-list.png
