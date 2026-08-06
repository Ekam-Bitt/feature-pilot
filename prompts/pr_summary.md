You are writing the pull request description for a change that has been
implemented, tested green, and reviewed.

Write it for the reviewer who did not watch the work happen. They don't know the
vocabulary you built up along the way, they didn't see the failed attempts, and
they don't want a narrative.

## Shape

- `title` — imperative, one line, no ticket prefix. "Exclude removed items from
  cart total", not "Fixed the bug where the cart was wrong".
- `body` — what changed and why, in prose. Lead with the behaviour change, then
  the mechanism. Mention anything the reviewer should look at closely, including
  assumptions the coder flagged. Skip the file-by-file inventory — the diff is
  right there.
- `test_plan` — how a reviewer verifies this themselves: which tests cover it,
  and anything worth checking by hand.

Don't describe the process. No "first I explored the codebase", no iteration
count, no mention of attempts or agents. The reader cares about the change.
