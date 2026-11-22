import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Answer } from "./Answer";

const ANSWER = [
  "Orders are priced in `price_order`.",
  "",
  "```mermaid",
  "flowchart LR",
  '  form["OrderForm"] -->|"POST /orders"| api["create_order"]',
  "```",
  "",
  "```python",
  "total = subtotal - discount",
  "```",
].join("\n");

describe("Answer", () => {
  it("turns mermaid blocks into diagrams and leaves other code blocks alone", () => {
    const html = renderToStaticMarkup(<Answer text={ANSWER} pending={false} />);

    expect(html.match(/class="mermaid-diagram"/g)).toHaveLength(1);
    // Before the diagram is drawn (effects do not run here) its source is shown as code.
    expect(html).toContain('<figure class="mermaid-diagram"><pre><code class="language-mermaid">flowchart LR');
    expect(html).toContain('<pre><code class="language-python">total = subtotal - discount\n</code></pre>');
    expect(html).toContain("<code>price_order</code>");
  });

  it("shows a half-streamed diagram as code", () => {
    const html = renderToStaticMarkup(<Answer text={"```mermaid\nflowchart TD\n  a -->"} pending />);
    expect(html).toContain('<code class="language-mermaid">flowchart TD\n  a --&gt;</code>');
  });

  it("renders answers without diagrams as plain markdown", () => {
    const html = renderToStaticMarkup(<Answer text={"| a | b |\n| - | - |\n| 1 | 2 |"} pending={false} />);
    expect(html).not.toContain("mermaid");
  });
});
