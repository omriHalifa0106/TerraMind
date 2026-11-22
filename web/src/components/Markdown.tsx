import DOMPurify from "dompurify";
import { marked } from "marked";
import { useMemo } from "react";

/**
 * The model's answer is Markdown. We render it to HTML and then sanitize it with DOMPurify,
 * because model output is untrusted text: without sanitizing, an injected <script> or
 * <img onerror> in an answer would run in the user's browser (XSS).
 */
export function Markdown({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(text, { async: false })), [text]);
  return <div className={`prose${streaming ? " streaming" : ""}`} dangerouslySetInnerHTML={{ __html: html }} />;
}
