.. _configuration file format:

===========================
 Configuration File Format
===========================

The configuration file for Mergify should be named ``.mergify.yml`` and must be
placed in the root directory of your GitHub repository. Mergify uses the
default repository branch configured on GitHub to read the configuration file —
usually ``master``. The file format is `YAML <http://yaml.org/>`_.

The file main entry is a dictionary whose key is named ``pull_request_rules``.
The value of the ``pull_request_rules`` key must be a list of dictionary.

Each dictionnary must have the following keys:

.. list-table::
   :header-rows: 1
   :widths: 1 1 2

   * - Key Name
     - Value Type
     - Value Description
   * - ``name``
     - string
     - The name of the rule. This is not used by the engine directly, but is
       used when reporting information about a rule.
   * - ``conditions``
     - array of :ref:`Conditions`
     - A list of :ref:`Conditions` string that must match against the pull
       request for the rule to be applied.
   * - ``actions``
     - dictionary of :ref:`Actions`
     - A dictionnary made of :ref:`Actions` that will be executed on the
       matching pull requests.

The rules are evaluated in the order they are defined and, therefore, the
actions are executed in that same order.

Example
=======

Here's a simple example of a configuration file:

.. code-block:: yaml

    pull_request_rules:
      - name: automatic merge when CI passes and 2 reviews
        conditions:
          - "#approved-reviews-by>=2"
          - status-success=continuous-integration/travis-ci
          - base=master
        actions:
          merge:
            method: merge

See :ref:`Examples` for more examples.
