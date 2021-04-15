.. meta::
   :description: Mergify Frequently Asked Questions
   :keywords: mergify, faq, questions, help

======
💬 FAQ
======

Mergify is unable to merge my pull request due to my branch protection settings
-------------------------------------------------------------------------------

This happens usually if you limit the people that have the authorization to
merge pull requests. To make it works with Mergify you must add ``Mergify`` to the
branch protection settings in the "`Restrict who can push to matching branches`" list.

.. image:: _static/mergify-push-user.png

Why did Mergify seem to have merged my pull request whereas all conditions were not true?
-----------------------------------------------------------------------------------------

The GitHub user interface might be misleading you in thinking that your pull
request has been merged by Mergify, while it wasn't.

If your pull request A has all its commits contained in another pull request B,
and if B is merged by Mergify, then A will appeared to have been merged by
Mergify too. GitHub detects that A's commits are a subset of B's commits and
therefore decides to automatically marks pull request A as merged.

Be sure that Mergify did nothing on the pull request A if it told you so in the
`Checks` tab. Don't be fooled by GitHub UI.

I see a rebase and force-push done by a member of the repository, but Mergify actually did it.
----------------------------------------------------------------------------------------------

See :ref:`strict rebase`.
