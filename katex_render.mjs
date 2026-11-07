// Reads a JSON array of {tex, display} from stdin and writes a JSON array of MathML strings.
// KaTeX is used only for its MathML output; the site shows MathML drawn with the Euler font.
import katex from "katex";

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  const items = JSON.parse(input || "[]");
  const out = items.map(({ tex, display }) => {
    try {
      return katex.renderToString(tex, {
        output: "mathml",
        displayMode: !!display,
        throwOnError: true,
      });
    } catch (err) {
      return `<code class="math-error" title="${String(err.message).replace(/"/g, "&quot;")}">${tex
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")}</code>`;
    }
  });
  process.stdout.write(JSON.stringify(out));
});
