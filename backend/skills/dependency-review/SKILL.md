---
name: dependency-review
description: Reviews declared package dependencies and looks for hidden coupling that never appears in a manifest. Use when the question is about dependencies, what uses or depends on a package or repository, hidden coupling (HTTP, queues, shared databases), or what breaks if something fails.
---

# Dependency review

Load `repo-navigation` as well if you still need the tool details.

The JSON index lists only **declared** dependencies from package manifests,
each with a kind of runtime, optional or dev, and the manifest that declares
it. Coupling that never appears in a manifest (HTTP calls between services,
shared databases, message queues) is not in the index.

1. Call `get_repository_info` (and `list_repositories` if needed) to read the
   declared dependencies and the packages each repository provides.
2. To see where a dependency is actually used, `search_code` for the package
   or import name and `read_file` at the hits. Remember that Ctags import
   lists can be empty, especially for JavaScript and TypeScript; a missing
   import in the index is not proof the code does not import it.
3. If the question turns on undeclared coupling, look for evidence with
   `search_code` and `read_file` (URLs, client constructors, queue or database
   names) and say plainly what you could and could not establish.
4. You cannot compute what breaks elsewhere when something fails. Reason from
   the declared dependencies and the usages you actually found, and say so.
   Do not invent a complete impact analysis.
