---
name: answer-with-citations
description: Grounds every factual claim in tool results and cites evidence as repo/path:line. Use for any question that states facts about the indexed repositories, and whenever you are about to write the answer.
---

# Answer with citations

Ground every factual claim in what the tools returned. Cite it inline as
`repo/path:line` or `repo/path:start-end`, using the locations the tools gave
you (search hits, read ranges, and symbol line numbers in the JSON index).

If the tools do not support an answer, say so instead of filling the gap from
general knowledge of similarly named projects. You may use general programming
knowledge only to decide what to search for, not as a substitute for evidence
from these repositories at their indexed revision.

Lead with the direct answer, then the supporting evidence. Keep answers as
short as the question allows. Do not invent line numbers or file paths.
