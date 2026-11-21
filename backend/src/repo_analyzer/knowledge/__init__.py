"""Static knowledge about the indexed repositories, stored as JSON.

Three artifacts are generated from one scan of each repository:

* the pack (``<repo>/pack.json``): every indexed file with its full text;
* the map (``<repo>/map.json``): each file's classes, functions and signatures;
* the graph (``graph.json``): repositories, files and symbols, and how they
  depend on, import and use one another across the whole workspace.

All three are deterministic functions of the repositories at their indexed
commits. No model and no embeddings are involved in producing them.
"""

SCHEMA_VERSION = 1
