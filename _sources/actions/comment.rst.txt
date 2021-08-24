.. meta::
   :description: Mergify Documentation for Comment Action
   :keywords: mergify, Comment, pull request
   :summary: Post a comment on a pull request.
   :doc:icon: comment

.. _comment action:

comment
=======

The ``comment`` action posts a comment to the pull request.

Options
-------

.. list-table::
   :header-rows: 1
   :widths: 1 1 1 3

   * - Key Name
     - Value Type
     - Default
     - Value Description
   * - ``message``
     - :ref:`data type template`
     -
     - The message to write as a comment.
   * - ``bot_account``
     - :ref:`data type template`
     -
     - |premium plan tag|
       Mergify can impersonate a GitHub user to comment a pull request.
       If no ``bot_account`` is set, Mergify will comment the pull request
       itself.

Examples
--------

🤜 Request for Action
~~~~~~~~~~~~~~~~~~~~~

If any event that requires the author of the pull request to edit its pull
request happen, you could write a rule that says something about it.

.. code-block:: yaml

    pull_request_rules:
      - name: ask to resolve conflict
        conditions:
          - conflict
        actions:
            comment:
              message: This pull request is now in conflicts. Could you fix it @{{author}}? 🙏

The same goes if one of your check fails. It might be good to give a few hints
to your contributor:

.. code-block:: yaml

    pull_request_rules:
      - name: ask to fix commit message
        conditions:
          - check-failure=Semantic Pull Request
          - -closed
        actions:
            comment:
              message: |
                Title does not follow the guidelines of [Conventional Commits](https://www.conventionalcommits.org).
                Please adjust title before merge.

💌 Welcoming your Contributors
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When somebody that's not part of your team creates a pull requests, it might be
great to give him a few hints about what to expect next. You could write him a
little message.

.. code-block:: yaml

    pull_request_rules:
      - name: say hi to contributors if they are not part of the regularcontributors team
        conditions:
          - author!=@regularcontributors
        actions:
            comment:
              message: |
                  Welcome to our great project!
                  We're delighted to have you onboard 💘


💬 Running CI pipelines automatically
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Some continuous integration systems allow you to trigger jobs by commenting on
the pull request. For example, Microsoft Azure allows that using
the `/AzurePipelines command
<https://docs.microsoft.com/en-us/azure/devops/pipelines/repos/github?view=azure-devops&tabs=yaml#comment-triggers>`_.
You could automate this using Mergify, and for example trigger the job after
other CI jobs have passed and at least one developer has reviewed the pull
request.

.. code-block:: yaml

    pull_request_rules:
      - name: run Azure CI job once ready to merge
        conditions:
          - "#approved-reviews-by>=1"
          - "check-success=ci/circleci: my_testing_job"
          - -closed
        actions:
          comments:
            message: /AzurePipelines run mypipeline

.. include:: ../global-substitutions.rst
