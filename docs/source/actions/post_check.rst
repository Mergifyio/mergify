.. meta::
   :description: Mergify Documentation for Post Check Action
   :keywords: mergify, post, check
   :summary: Post custom status check to a pull request.
   :doc:icon: check


.. _post_check action:

post_check
==========

|premium plan tag|

The ``post_check`` action adds an item in a pull request check list. The check
status is ``success`` when all conditions match, otherwise, it is set to
``failure``.


.. list-table::
  :header-rows: 1
  :widths: 1 1 1 2

  * - Key Name
    - Value Type
    - Default
    - Value Description

  * - ``title``
    - :ref:`data type template`
    -
    - The title of the check.

  * - ``summary``
    - :ref:`data type template`
    -
    - The summary of the check.


As the ``title`` and ``summary`` use on :ref:`data type template`, you can
benefit from any pull request attributes e.g. ``{{author}}`` and also these
additional variables:

    * ``{{ check_rule_name }}`` the name of the rule that triggered this action.
    * ``{{ check_succeed }}`` is ``True`` if all conditions matches otherwise ``False``
    * ``{{ check_conditions }}`` the list of all conditions with a checkbox marked if the condition match

.. include:: ../global-substitutions.rst
