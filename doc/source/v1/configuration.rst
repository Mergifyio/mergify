==================
Configuration file
==================

The file format for ``mergify.yml`` is fully described here. The main entry is
a dict whose key is named ``rules``. The value of the ``rules`` key must be a
dict with the following optional keys:

.. list-table::
   :header-rows: 1

   * - Key name
     - Value type
     - Value description
   * - ``default``
     - :ref:`rule`
     - The rules to be applied by Mergify by default. See :ref:`rule` for the
       rule format. Or ``null`` to disable Mergify by default.

   * - ``branches``
     - ``dict``
     - A dictionary where keys are string or regular expression (if
       starting by ``^``) to match against branch names and the values are of
       type :ref:`rule` or ``null`` to disable Mergify on this branch.

.. _rule:

Rule
====

A ``Rule`` is a dictionary with the following optional keys:

.. list-table::
   :header-rows: 1

   * - Key name
     - Value type
     - Value description
   * - ``protection``
     - :ref:`Protection`
     - The protection to be applied on pull requests.
   * - ``disabling_label``
     - ``string``
     - A `label <https://help.github.com/articles/about-labels/>`_ name that
       will disable Mergify if it is applied to a pull request. Default is
       ``no-mergify``.
   * - ``enabling_label``
     - ``string``
     - A `label <https://help.github.com/articles/about-labels/>`_ name that
       will enable Mergify only if it is applied to a pull request. Default is
       ``null``.
   * - ``disabling_files``
     - ``array of string``
     - A list of files paths that disables Mergify if they are part of the pull
       request. Any file that changes the behavior of the continuous integration
       workflow should be listed. The default is ``[.mergify.yml]``. Note that
       the engine always ignore pull requests that modify ``.mergify.yml``
       anyway. Those pull requests will have to be merged manually, even if
       `.mergify.yml` is not listed in this setting.
   * - ``merge_strategy``
     - :ref:`Merge Strategy`
     - The method to use to merge pull requests.
   * - ``automated_backport_labels``
     - ``dict``
     - A dictionary where keys are `labels
       <https://help.github.com/articles/about-labels/>`_ name and values are
       branch name. When a pull request is merged and labeled with one of the
       keys, a new pull request is created on the branch associated to the
       key. The new pull request have all commits cherry-picked from the
       original pull request.

.. _Merge Strategy:

Merge Strategy
==============

A ``Merge Strategy`` defines the method to merge pull requests. It is a
dictionary with the following optional keys:

.. list-table::
   :header-rows: 1


   * - Key name
     - Value type
     - Value description
   * - ``method``
     - ``string``
     - Merge method to use. Possible values are ``merge``, ``squash`` or
       ``rebase``. Default is ``merge``.
   * - ``rebase_fallback``
     - ``string``
     - If ``method`` is set to ``rebase``, but the pull request cannot be
       rebased, the method defined in ``rebase_fallback`` will be used instead.
       Possible values are ``merge``, ``squash``, ``none``. Default is
       ``merge``.

.. _protection:

Protection
==========

A ``Protection`` defines the safeguards that are needed to merge pull requests
in a branch. It is a dictionary with the following optional keys:

.. list-table::
   :header-rows: 1

   * - Key name
     - Value type
     - Value description
   * - ``required_status_checks``
     - :ref:`Required Status Checks`
     - Require status checks to pass before merging. Set to ``null`` to disable.
   * - ``required_pull_request_reviews``
     - :ref:`Required Reviews`
     - The required criterias on code reviews for the pull request to be
       merged. Set to ``null`` to disable.
   * - ``restrictions``
     - :ref:`Restriction`
     - Restrict who can push to this branch. Team and user restrictions are
       only available for organization-owned repositories. Set to ``null`` to
       disable.
   * - ``enforce_admins``
     - ``boolean``
     - Enforce all configured restrictions for administrators. Set to ``true``
       to enforce required status checks for repository administrators. Set to
       ``false`` to disable.

.. _required status checks:

Required Status Checks
======================

A ``Required Status Checks`` defines which other services must report a valid
status check before a pull request can be merged. It is a dictionary with the
following optional keys:

.. list-table::
   :header-rows: 1

   * - Key name
     - Value type
     - Value description
   * - ``strict``
     - ``boolean``
     - Require branches to be up to date before merging. See
       :doc:`../strict-workflow`.
   * - ``contexts``
     - ``array``
     - The list of status checks to require in order to merge into this branch

.. _required reviews:

Required Reviews
================

A ``Required Reviews`` defines what kind of `reviews
<https://help.github.com/articles/about-pull-request-reviews/>`_ are needed for
a pull request to be merged. It is a dictionary with the following optional
keys:

.. list-table::
   :header-rows: 1

   * - Key name
     - Value type
     - Value description
   * - ``dismiss_stale_reviews``
     - ``boolean``
     - Set to ``true`` if you want to automatically dismiss approving reviews
       when someone pushes a new commit.
   * - ``require_code_owner_reviews``
     - ``boolean``
     - Blocks merging pull requests until `code owners
       <https://help.github.com/articles/about-codeowners/>`_ review them.
   * - ``required_approving_review_count``
     - ``integer``
     - Specify the number of reviewers required to approve pull requests. Use a
       number between 1 and 6. If you don't need any reviewer, set
       ``required_pull_request_reviews`` to ``null`` instead.

.. _restriction:

Restriction
===========

A ``Restriction`` defines a list of user or team for whom a restriction
applies. It is a dictionary with the following optional keys:

.. list-table::
   :header-rows: 1

   * - Key name
     - Value type
     - Value description
   * - ``users``
     - ``array``
     - The list of user logins with push access.
   * - ``teams``
     - ``array``
     - The list of team slugs with push access


.. _default:

Default branch configuration
============================

The default configuration for all branches is the following:

.. literalinclude:: ../../../mergify_engine/data/default_rule.yml
