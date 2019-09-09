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


Mergify is unable to merge my pull request due to my branch protection settings
-------------------------------------------------------------------------------

This happens usually if you limit the people that have the authorization to
merge pull requests. GitHub does not offer any way to authorize GitHub
Application (such as Mergify) to merge pull requests if you decide to set up
branch protection.

You should not use "Restrict who can push to matching branches" if you want to
use Mergify merge action.


Why did Mergify seem to have merged my pull request whereas all conditions were not true?
-----------------------------------------------------------------------------------------

The GitHub user interface might be misleading you in thinking that your pull
request has been merged by Mergify, while it wasn't.
If your pull request A has all its commits contained in another pull request B,
and if B is merged by Mergify, then A will appeared to have been merged by
Mergify too. GitHub detects that A's commits are a subset of B's commits and
therefore decides to automatically marks pull request A as merged.

Be sure that Mergify did nothing on the pull request A if it told you so in the
Checks tab. This is GitHub UI magic.

.. _`faq strict rebase`:

I see a rebase and force-push done by a member of the repository, but Mergify actually did it.
----------------------------------------------------------------------------------------------

If you are using :ref:`strict merge` with ``strict_method`` set to ``rebase``,
Mergify will rebase your pull request to get it up-to-date with its base
branch. The branch needs to be then force-pushed to the pull request owner
branch.

GitHub Applications are not allowed to do that so. This is a GitHub limitation
that we already have reported.

In order to make this works, Mergify borrows a token from one of your
repository member and uses it to force-push the rebased branch.
