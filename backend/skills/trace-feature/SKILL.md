---
name: trace-feature
description: Traces a feature, request, or behaviour through the code from a starting name to the functions and files that implement it. Use when the question is where something is handled, how it works end-to-end, follow this request, or asks to trace a flow or feature across files or repositories.
---

# Trace a feature

Load `repo-navigation` as well if you still need the tool details.

1. Start from the words the question uses and the identifiers the code is
   likely to use. Call `list_repositories` if the workspace is unfamiliar.
2. Use `get_repository_info` (with `path` when the repository is large) to
   find matching functions, classes and files.
3. `search_code` for those identifiers, imports and call sites, across
   repositories when the feature might cross a boundary. If a query misses,
   try other spellings before concluding the code is absent.
4. `read_file` at the hits to follow the flow: callers, callees, configuration
   and error paths. Repeat until you can describe the path with evidence.
5. Write the trace as a short sequence of steps, each cited as
   `repo/path:line`. Say plainly if a step could not be established from the
   tools.
