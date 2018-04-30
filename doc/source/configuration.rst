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
       rule format.

   * - ``branches``
     - ``dict``
     - A dictionary where keys are regular expression to match against branch
       names and the values are of type :ref:`rule`.

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
       merged.
   * - ``restrictions``
     - :ref:`Restriction`
     - Restrict who can push to this branch. Team and user restrictions are
       only available for organization-owned repositories. Set to ``null`` to
       disable.
   * - ``enforce_admins``
     - ``boolean``
     - Enforce all configured restrictions for administrators. Set to ``true``
       to enforce required status checks for repository administrators. Set to
       ``null`` to disable.

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
     - Require branches to be up to date before merging.
   * - ``contexts``
     - ``array``
     - The list of status checks to require in order to merge into this branch

.. _required reviews:

Required Reviews
================

A ``Required Reviews`` defines what kind of reviews are needed for a pull
request to be merged. It is a dictionary with the following optional keys:

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
     - Blocks merging pull requests until code owners review them.
   * - ``required_approving_review_count``
     - ``integer``
     - Specify the number of reviewers required to approve pull requests. Use a
       number between 1 and 6.

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


Default branch configuration
============================

The default configuration for all branches is the following:

.. literalinclude:: ../../default_rule.yml
