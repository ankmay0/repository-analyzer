---
name: trace-feature
description: Traces a feature, request, or behaviour through the code from a starting name to the functions and files that implement it, including an HTTP request from its router down to the database. Use when the question is where something is handled, how it works end-to-end, follow this request or endpoint, or asks to trace a flow or feature across files or repositories.
---

# Trace a feature

Load `repo-navigation` as well if you still need the tool details.

1. Start from the words the question uses and the identifiers the code is
   likely to use. Call `list_repositories` if the workspace is unfamiliar.
2. **HTTP endpoints: use the code graph first.** If the question names or
   implies an endpoint (`POST /orders`, "what happens when an order is
   placed") and the `trace_endpoint` tool is available, call it with the
   repository, method and path. It returns the handler and the functions it
   calls, in call order, each with a `repo/path:start-end` range and the
   tables each one reads or writes.
   - `read_file` the ranges on the path, in order, and explain what each step
     does to the data: validation, calculations and rounding, caching, what is
     written where. The graph says where; the code says how.
   - The trace comes from static analysis. Where it lists gaps, stops before
     reaching a table, or the code you read calls something the trace does not
     show, follow that part with steps 3 to 5.
   - Use `callers_of` and `callees_of` to walk further up or down from any
     function.
   If `trace_endpoint` is not available, or reports no matching endpoint (use
   the list it returns to pick the right route), continue with step 3.
3. Use `get_repository_info` (with `path` when the repository is large) to
   find matching functions, classes and files.
4. `search_code` for those identifiers, imports and call sites, across
   repositories when the feature might cross a boundary. If a query misses,
   try other spellings before concluding the code is absent.
5. `read_file` at the hits to follow the flow: callers, callees, configuration
   and error paths. Repeat until you can describe the path with evidence.
6. Write the trace as a Mermaid flowchart of the path (load `draw-diagrams`
   for the conventions) followed by a short sequence of steps, each cited as
   `repo/path:line`, with the data transformation at each step. Say plainly if
   a step could not be established from the tools.
