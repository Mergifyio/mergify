===
FAQ
===

How do I fix the *"Pull request can't be updated with latest base branch changes, owner doesn't allow modification"* error?
---------------------------------------------------------------------------------------------------------------------------

When :doc:`strict-workflow` is enabled, pull requests must be updated with the
latest content from the target branch before being merged. To do that, Mergify
needs the permission to update the source branch of the pull request.

In certain cases, the pull request creator might not have authorized the
repository owners to modify its source branch, which will block Mergify with
this error. The solution is for the author of the pull request to either update
manually its branch to the target branch (via a ``git merge`` or a ``git
rebase``) or to give permission for Mergify to do it as described in `the
GitHub documentation
<https://help.github.com/articles/allowing-changes-to-a-pull-request-branch-created-from-a-fork/>`_.

.. _migration-v2:

How do I migrate from engine v1 to engine v2?
---------------------------------------------

You just need to update your configuration file to the new format with the
following command:

.. code:: bash

    $ cd <my_project>
    $ curl -F 'data=@.mergify.yml' https://gh.mergify.io/convert


Or by uploading the configuration file with this form:

.. raw:: html

    <form method=post enctype=multipart/form-data action=https://gh.mergify.io/convert target=_blank>
      <input type=file name=data>
      <input type=submit value=Convert>
    </form>


