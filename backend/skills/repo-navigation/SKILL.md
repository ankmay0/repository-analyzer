---
name: repo-navigation
description: Shows how to inspect indexed repositories with the four repository tools (list_repositories, get_repository_info, search_code, read_file), including the JSON index shape, Universal Ctags caveats, and exact-text search tips. Use when exploring a workspace, looking up files, symbols or structure, searching or reading code, or when unsure which tool to call first.
---

# Repository navigation

You have no built-in knowledge of these repositories at their indexed revision.
Everything you state about them must come from the tools below.

## Tools

- **list_repositories**: what is indexed, with owner, branch, commit and size.
  Start here if you do not yet know what is available.
- **get_repository_info**: a repository's JSON index. It has the basic info, the
  file tree (path, language, size, line count), the dependencies declared in
  package manifests (runtime, optional or dev), and the functions, classes and
  imports of each source file with line numbers. Use it to find your way around
  and to answer questions about structure and declared dependencies. On a large
  repository pass `path` (a directory or file) to see the symbols, which are
  otherwise left out to keep the result short.
- **search_code**: exact text or regular-expression search over the files, not
  semantic search. Use the words the code itself would use: identifiers, import
  statements, package names, URLs, configuration keys. If a first query finds
  nothing, try other likely spellings before concluding the code is absent. You
  can restrict the search with `repo` and `path`, match case-insensitively, or
  pass `regex` for a Python regular expression.
- **read_file**: read a file or line range once you know where to look. At most
  400 lines are returned per call; request another range if you need more.

## Code graph tools (only when the code graph is enabled)

- **trace_endpoint**: an HTTP endpoint's path from its handler through the
  functions it calls to the tables they read or write, with line ranges.
- **callers_of** / **callees_of**: who calls a function, and what it calls.

They cover Python with FastAPI routes and are built by static analysis: calls
through variables, dependency injection or dynamic dispatch are missing, and
gaps are listed. Use them to find where to read, then confirm with `read_file`.
If they are not in your tool list, use `search_code` and `read_file`.

## JSON index and symbols

Functions, classes and imports in the JSON come from Universal Ctags. Import
lists are only as complete as Ctags makes them (often empty for JavaScript and
TypeScript). Arrow functions assigned to `const` are usually tagged as
constants and omitted. TypeScript class methods in `.tsx` files are often
missing. When the index does not list a symbol or import you expect, search the
text or read the file rather than assuming it is absent from the code.

## Search tips

- Prefer an exact identifier (`create_session`, `sign_token`) over a paraphrase
  ("how login works").
- If a name might be spelled several ways, try each before giving up.
- Use `path` or `repo` to narrow a noisy result, and a regular expression when
  a literal string is too broad or too narrow.
