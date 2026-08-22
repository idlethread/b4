integrate: batch-building integration branches
===============================================
The ``integrate`` subcommand rebuilds a set of git branches from lists of
in-flight patch series tracked by message-id, applying each series with
the same machinery as ``b4 shazam``.

It is aimed at maintainers and integrators who assemble a working tree out
of several series that are still being revised on the mailing list -- for
example, a board-enablement or feature-integration tree that pulls together
a driver series, a devicetree series, and a documentation series that each
live in a separate thread. Instead of re-running ``b4 shazam`` by hand for
every series every time one of them is rerolled, you keep the set of series
in a small YAML file and rebuild all of the branches with a single command.

.. versionadded:: v0.16

How it works
------------
You describe the branches you want in a YAML file that maps each branch
name to an ordered list of message-ids::

    integration:
      - <20260115-driver-series-v3-0-abc@example.org>
      - <20260116-dts-series-v2-0-def@example.org>
    docs:
      - <20260117-docs-series-v1-0-ghi@example.org>

Each entry may be given either as a bare message-id or as a full
lore.kernel.org URL -- for example, pasting
``https://lore.kernel.org/all/<msgid>/`` (or a ``/r/`` or per-list link, with
or without a trailing ``/T/#u`` thread anchor) works just as well as the bare
``<msgid>``. b4 strips the lore prefix down to the raw message-id when it
loads the file, so the two forms are interchangeable.

Then point ``b4 integrate`` at it::

    $ b4 integrate series.yaml

For each branch in the file, b4:

1. Checks out the base ref (``HEAD`` by default, or ``--base REF``).
2. Creates or resets the branch with ``git checkout -B <branch>``.
3. Applies each message-id in the list, in order, through ``b4 shazam``.

Branches are processed independently, so a failure while building one
branch does not prevent the others from being built. Each branch always
starts from the same clean base snapshot.

The working tree is a scratch space
-----------------------------------
``b4 integrate`` treats your working tree as disposable. Before it starts,
it records the current branch (or commit, if you are in detached-HEAD
state) and the current ``HEAD`` commit. After every branch -- whether it
succeeded, was skipped, or failed -- it restores that snapshot:

* any in-progress ``git am`` is aborted,
* the original branch is checked out,
* the tree is ``git reset --hard`` back to the original commit, and
* untracked files are removed with ``git clean -fd``.

.. warning::

   Because each branch is reset with ``git checkout -B`` and the working
   tree is restored with ``git reset --hard`` and ``git clean -fd``, run
   ``b4 integrate`` only in a repository whose working tree you are willing
   to have wiped. Commit or stash anything you care about first, and do not
   point ``--base`` or a branch name at work you have not backed up.

Handling apply failures
------------------------
When a series does not apply cleanly (a conflict, a missing base commit,
etc.), ``b4 shazam`` leaves the repository mid-``git am`` and b4 stops to
ask you what to do:

.. code-block:: text

    Options:
      [c] Continue after fixing manually
      [r] Retry this message-id
      [s] Skip this message-id
      [a] Abort branch
    >

``[c] Continue``
  Use this after you have resolved the conflict in another terminal and
  finished the ``git am`` (for example with ``git am --continue``). b4
  records the message-id as applied and moves on to the next one. If a
  ``git am`` is still in progress, b4 tells you to finish it first and
  re-prompts.

``[r] Retry``
  Re-run the same message-id, for instance after you have changed the base
  or fetched a fresh copy of the thread.

``[s] Skip``
  Move on to the next message-id in the list without recording this one.
  The branch is still counted as successfully built.

``[a] Abort``
  Give up on this branch. It is recorded as failed, the snapshot is
  restored, and b4 continues with the next branch.

When b4 is run without an interactive terminal, an apply failure aborts the
current branch (equivalent to ``[a]``) rather than blocking on input, so
the branch is bucketed as failed and the run continues.

Compile-testing each branch with ``--compile-test``
---------------------------------------------------
A branch that applies cleanly can still fail to build once several series are
stacked on top of one another. Pass ``--compile-test CMD`` to run a shell
command once after each branch is fully built; a non-zero exit fails that
branch (it is bucketed with the apply failures in the summary) while the run
continues with the next branch::

    $ b4 integrate series.yaml --compile-test 'make -j$(nproc)'

The command runs through the shell, so pipelines, ``&&`` chains, and shell
variables all work. b4 exports the name of the branch being built as
``$B4_BRANCH`` in the command's environment, which lets a single command
select the right configuration per branch -- for example choosing a board
defconfig keyed off the branch name::

    $ b4 integrate boards.yaml --compile-test './ci/build-board.sh "$B4_BRANCH"'

The compile-test runs after the whole branch is assembled, not after every
individual patch, so it verifies the final integrated tree. As with any other
failure, the working tree is restored to its original snapshot afterwards.

Keeping the YAML up to date with ``--update-config``
----------------------------------------------------
Series get rerolled, and the message-id in your YAML file eventually points
at an older revision. With ``--update-config``, after each series applies
successfully b4 walks the lore thread forward to find the latest revision
of that series and, if a newer one exists, records its message-id.

At the end of the run b4 rewrites the YAML file in place with the newer
message-ids, matching entries by branch name and preserving branch order.
The rewrite is line-wise rather than a full YAML dump: only the value on a
list-item line whose message-id actually changed is edited, so any comments
you keep in the file -- for example a ``# <series title> — <author>`` line
above each entry -- survive untouched, along with blank lines and quoting
style. The rewrite is atomic (b4 writes a ``.new`` file and renames it over
the original), and if no newer revisions were found the file is left
untouched::

    $ b4 integrate series.yaml --update-config
    ...
      integration: <20260115-driver-series-v3-0-abc@example.org> → <20260201-driver-series-v4-0-xyz@example.org>
    Updated series.yaml

.. note::

   Finding newer revisions requires network access to the public-inbox
   server, and revision discovery uses the same change-id / thread-walking
   logic as the rest of b4. If a newer revision cannot be found, the
   original message-id is kept.

The summary report
-------------------
At the end of every run b4 prints a summary grouping the branches into
those that built successfully, those that were skipped, and those that
failed (with the reason)::

    Integration summary
    Successful branches: 1
      ✓ integration
    Skipped branches: 1
      - docs
    Failed branches: 1
      ✗ experimental: shazam failed for <20260117-series-v1-0-ghi@example.org>

A branch is *skipped* when its message-id list is empty. A branch *fails*
when a series could not be applied and you chose ``[a] Abort`` (or the run
was non-interactive).

Options
-------
``yaml_file``
  Path to the YAML file mapping branch names to ordered lists of
  message-ids. The top level must be a mapping, and every value must be a
  list; b4 rejects the file otherwise.

``--base REF``
  Git ref used as the starting point for every branch. Defaults to
  ``HEAD``. Each branch is created with ``git checkout -B <branch>`` from
  this ref, so all branches share the same base.

``--update-config``
  After each successful apply, look for a newer revision of the series on
  the public-inbox server and rewrite the YAML file in place with any newer
  message-ids. Without this flag the YAML file is never modified.

``--compile-test CMD``
  Shell command run once after each branch is built, with the branch name
  exported as ``$B4_BRANCH``. A non-zero exit fails that branch (bucketed with
  the apply failures) and the run moves on to the next branch. Useful for
  confirming the integrated tree actually compiles.

``--only BRANCH``
  Process only the named branch instead of every branch in the file. The
  option is repeatable (``--only a --only b``) and branch names that do not
  appear in the config are warned about and ignored. Branches are still
  processed in the order they appear in the YAML file, not in the order the
  ``--only`` flags are given. When combined with ``--update-config``, the
  branches you did *not* select are left untouched in the rewritten file, so
  it is safe to refresh one branch at a time.

Examples
--------
Rebuild all branches described in a config file on top of the current
``HEAD``::

    $ b4 integrate series.yaml

Rebuild them on top of a specific tag instead of the current ``HEAD``::

    $ b4 integrate series.yaml --base v6.14-rc1

Rebuild the branches and refresh the YAML file to point at the latest
revisions of each series::

    $ b4 integrate series.yaml --update-config

Rebuild just one branch out of a larger file (repeat ``--only`` for more)::

    $ b4 integrate series.yaml --only glymur

Rebuild every branch and compile-test each one, selecting a board defconfig
from the branch name::

    $ b4 integrate boards.yaml --compile-test './ci/build-board.sh "$B4_BRANCH"'
