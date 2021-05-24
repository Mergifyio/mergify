.. meta::
   :description: Mergify Documentation for Configuration
   :keywords: mergify, configuration

.. _configuration file format:

=====================
🔖 Configuration File
=====================

File Used
---------

Mergify uses the configuration file that is:

- The file named ``.mergify.yml``, or, as a fallback, ``.mergify/config.yml`` or ``.github/mergify.yml``
  from the root directory.

- In the default repository branch configured on GitHub — usually ``main``.

Format
------

Here's what you need to know about the file format:

- The file format is `YAML <http://yaml.org/>`_.

- The file main type is a dictionary whose keys are named
  ``pull_request_rules`` and ``defaults``.

Pull Request Rules
~~~~~~~~~~~~~~~~~~

- The value type of the ``pull_request_rules`` is list.

- Each entry in ``pull_request_rules`` must be a dictionary.

Each dictionary must have the following keys:

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
     - list of :ref:`Conditions`
     - A list of :ref:`Conditions` string that must match against the pull
       request for the rule to be applied.
   * - ``actions``
     - dictionary of :ref:`Actions`
     - A dictionary made of :ref:`Actions` that will be executed on the
       matching pull requests.

The rules are evaluated in the order they are defined in ``pull_request_rules``
and, therefore, the actions are executed in that same order.

See :ref:`Examples` for configuration file examples.

Queue Rules
~~~~~~~~~~~

- The value type of the ``queue_rules`` is list.

- Each entry in ``queue_rules`` must be a dictionary.

See :ref:`queue rules` for the complete list and description of options.

Defaults
~~~~~~~~

- The value type of ``defaults`` is a dictionary.

This dictionary must have the following key:

.. list-table::
   :header-rows: 1
   :widths: 1 1 2

   * - Key Name
     - Value Type
     - Value Description
   * - ``actions``
     - dictionary of :ref:`Actions`
     - A dictionary made of :ref:`Actions` whose configuration will be used by default.

The ``defaults`` section is used to define default configuration valued for actions run by pull request rules and by :ref:`Commands`.
If the options are defined in ``pull_request_rules`` they are used, otherwise, the values set in ``defaults`` are used.

For example:

.. code-block:: yaml

  defaults:
    actions:
      comment:
        bot_account: Autobot

  pull_request_rules:
    - name: comment with default
      conditions:
        - label=comment
      actions:
        comment:
          message: I 💙 Mergify

The configuration above is the same as below:

.. code-block:: yaml

  pull_request_rules:
    - name: comment with default
      conditions:
        - label=comment
      actions:
        comment:
          message: I 💙 Mergify
          bot_account: Autobot

Data Types
----------

.. _regular expressions:

Regular Expressions
~~~~~~~~~~~~~~~~~~~

.. tip::

   You can use `regex101 <https://regex101.com/>`_, `PyRegex
   <http://www.pyregex.com>`_ or `Pythex <https://pythex.org/>`_ to test your
   regular expressions.

You can use regular expression with matching :ref:`operators <Operators>` in
your :ref:`conditions <Conditions>` .

Mergify leverages `Python regular expressions
<https://docs.python.org/3/library/re.html>`_ to match rules.


Examples
++++++++

.. code-block:: yaml

    pull_request_rules:
      - name: add python label if a Python file is modified
        conditions:
          - files~=\.py$
        actions:
          label:
            add:
              - python

      - name: automatic merge for main when the title does not contain “WIP” (ignoring case)
        conditions:
          - base=main
          - -title~=(?i)wip
        actions:
          merge:
            method: merge

.. _data type template:

Template
~~~~~~~~

.. note::

   You need to replace the ``-`` character by ``_`` from the :ref:`pull request
   attribute <attributes>` names when using templates. The ``-`` is not a valid
   character for variable names in Jinja2 template.

The template data type is a regular string that is rendered using the `Jinja2
template language <https://jinja.palletsprojects.com/templates/>`_.

If you don't need any of the power coming with this templating language, you
can just use this as a regular string.

However, those templates allow to use any of the :ref:`pull request attribute
<attributes>` in the final string.

For example the template string:

.. code-block:: jinja

    Thank you @{{author}} for your contribution!

will render to:

.. code-block:: jinja

    Thank you @jd for your contribution!

when used in your configuration file — considering the pull request author
login is ``jd``.


Validation
----------

Changes to the configuration file should be done via a pull request in order
for Mergify to validate it via a GitHub check.

However, if you want to validate your configuration file before sending a pull
request, you can use the following command line:

.. code:: bash

    $ curl -F 'data=@.mergify.yml' https://gh.mergify.io/validate


Or by uploading the configuration file with this form:

.. raw:: html

    <form method=post enctype=multipart/form-data action=https://gh.mergify.io/validate target=_blank>
      <input type=file name=data>
      <input type=submit value=Validate>
    </form>
