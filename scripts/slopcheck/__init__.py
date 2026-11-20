"""slopcheck: plainpy's own checks for over-abstraction, using only the stdlib `ast`.

- `edit_rules`: single-file rules that check_file runs after every edit (SLP002, SLP004, SLP012).
- `finish_rules`: project rules and budgets that gate runs when Claude finishes, over every
  Python file in the project (SLP001, SLP003, SLP005 to SLP008, SLP010, SLP011).
- `classes`: full class names (module and `__qualname__`) and base-class resolution.
- `shared`: parsing, allow comments and small AST helpers.

SLP009 is basedpyright's, kept by gate. Test files and stubs are never judged. A rule can be
allowed on one definition with a comment on its `def` or `class` header (any line of it,
since the formatter may wrap a long header), or on the first line of a module-level SLP012
table: `# slopcheck: allow SLP004 -- reason of three or more words`.
"""
