---
name: explain-architecture
description: Explains how a repository or set of repositories is organised: structure, entry points, and how the parts fit together. Use when the question is about architecture, structure, overview, module layout, or how the project works.
---

# Explain architecture

Load `repo-navigation` as well if you still need the tool details.

1. If you do not know what is indexed, call `list_repositories`.
2. Call `get_repository_info` for each repository that matters. Use the file
   tree, language counts, declared dependencies and symbols to outline the
   shape of the code. On a large repository, pass `path` to inspect one area
   at a time.
3. Identify likely entry points (a `main` function, an application factory, a
   public API, a documented module) from the index, then `search_code` and
   `read_file` to confirm how they wire the rest together.
4. Describe the architecture from that evidence, with `repo/path:line`
   citations. If symbols or imports are missing from the index (Ctags is
   incomplete for some languages), fall back to search and file reads rather
   than guessing.
5. Show how the main parts connect as a small Mermaid flowchart (load
   `draw-diagrams` for the conventions), then explain it in prose with
   citations. Do not describe the architecture as a README-style table.
