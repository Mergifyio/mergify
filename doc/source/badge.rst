.. _Badge:

======
 Badge
======

Mergify offers 3 badges that you can use in your project to inform
contributors that your repository is managed by Mergify.

Three styles are available `cut` (default), `small` and `big`:

|cut| |small| |big|

In a `README.md`:

.. code-block:: md

    # My project name [![Mergify Status][mergify-status]][mergify]

    [mergify]: https://mergify.io
    [mergify-status]: https://gh.mergify.io/badges/:owner/:repo.png?style=cut


In a `README.rst`:

.. code-block:: rst

    ===============
    My project name
    ===============

    .. image:: https://gh.mergify.io/badges/:owner/:repo.png?style=cut
       :target: https://mergify.io
       :alt: Mergify Status


.. |cut| image:: _static/badge-enabled-cut.png
.. |small| image:: _static/badge-enabled-small.png
.. |big| image:: _static/badge-enabled-big.png
