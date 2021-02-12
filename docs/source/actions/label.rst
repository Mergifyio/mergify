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
