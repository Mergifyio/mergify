===============
Getting Started
===============

Installation
------------

In order to work, Mergify needs access to your account and to be enabled. To do
so, start by logging in using your GitHub account at
https://mergify.io/dashboard. On first login, you will be asked to give
some permissions on your behalf for Mergify to work.

Once this is done, you need to enable the Mergify GitHub Application on the
repositories you want. Go to https://github.com/apps/mergify/installations/new
and enroll repositories where you want Mergify to be enabled.

Configuration
-------------

In order for Mergify to apply your rules to your pull requests, you need to
create a configuration file. The configuration file should be created in the
root directory of each enabled repository and named ``.mergify.yml``.

Here's how to create a file with a minimal content to enable Mergify:

.. code-block:: shell

    $ cd myrepository
    $ echo "rules:" > .mergify.yml
    $ git add .mergify.yml
    $ git commit -m "Enable mergify.io"
    $ git push

Since this file does not contain any specific rules, the :ref:`default branch
configuration <default>` will be used. By default, Mergify will merge any pull
request that has one approval review.

A more realistic example of the ``.mergify.yml`` file would look like :

.. code-block:: yaml

    rules:
      default:
        protection:
          required_status_checks:
            contexts:
              - continuous-integration/travis-ci
          required_pull_request_reviews:
            required_approving_review_count: 2

The key ``default`` stores the default merging rules to use on all branches.
The ``required_status_checks`` lists all checks that must pass before merging a
pull request. The ``required_pull_request_reviews`` defines how many reviewer
of the repository should approve the pull request before it gets merged.

In this case, two reviewers must approve the pull request and the Travis CI
must pass before Mergify merges the pull request.

You can define a per branch rule using the ``branches`` settings. To match
multiple branches at once, you can use a regular expression:

.. code-block:: yaml

    rules:
      default:
        protection:
          required_status_checks:
            contexts:
              - continuous-integration/travis-ci
          required_pull_request_reviews:
            required_approving_review_count: 2
      branches:
        protection:
          stable/.*:
            required_pull_request_reviews:
              required_approving_review_count: 1


In this example, pull requests sent to a branch whose name matches ``stable/*``
will only require one review approval and will still require Travis to pass.

You can read the :doc:`full list of configuration option <configuration>` for
more information.

Mergify is now ready, what will happen next?
--------------------------------------------

When a contributor sends a pull request to the repository, Mergify will post a
status check about the state of the pull request according to the defined
rules.

.. image:: _static/mergify-status-ko.png
   :alt: status check

.. note::

   When a pull request changes the configuration of Mergify, the ``mergify/pr``
   status is built with the current configuration (without the pull request
   change). To validate the Mergify configuration change a additional status is
   posted named ``mergify/future-config-checker``

When all the criterias of the rules are satisfied, Mergify will merge the base
branch into the pull request if the pull request is not up-to-date with the
base branch. This is made to ensure that the pull request is tested one last
time while being up-to-date with the base branch.

Once the required services status are approved, Mergify will automatically
merge the pull request:

.. image:: _static/mergify-merge.png
   :alt: merge

You can follow the state of the Mergify merge queues by connecting to `your
dashboard <https://mergify.io/dashboard>`_

Now, that Mergify. is setup, you can go back on what matters for your project
and let us babysit your pull requests!
