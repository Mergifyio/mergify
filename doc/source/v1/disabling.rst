===================
 Disabling Mergify
===================

Sometimes, you don't want to have Mergify in your way. That's understandable —
there are always a few corner cases where automation could get in your way.

Disabling for some Pull Requests
--------------------------------

You can use the ``disabling_label`` option in :ref:`rule`. This is a GitHub
`label <https://help.github.com/articles/about-labels/>`_ name that when
present disables Mergify. The default label name is ``no-mergify``. Just create
this label if it does not exist in your repository, and apply to any pull
request you want Mergify to ignore.

.. code-block:: yaml

    rules:
      default:
        disabling_label: no-mergify


Disabling for Sensitive Files
-----------------------------

Use the ``disabling_files`` option in :ref:`rule`:. If any of the files listed
in this option is modified by the pull request, Mergify ignores the pull
request. That is especially handy if you don't want to manually merge some pull
requests that modify sensitive files.

.. code-block:: yaml

    rules:
      default:
        disabling_files:
          - credentials.txt
          - prod-config.yaml

Manual activation
-----------------

You can use the `enabling_label` option :ref:`rule`: to only activate Mergify
on demand. Again, this is a GitHub `label
<https://help.github.com/articles/about-labels/>`_ name that when absent,
disables Mergify.

That option is especially useful if you want to have a two-phases approval: for
example, you can force the pull request to have at least one reviewer while not
having it merged unless the ``ready-to-be-merged`` label is present:

.. code-block:: yaml

    rules:
      default:
        protection:
          required_approving_review_count: 1
        enabling_label: ready-to-be-merged
