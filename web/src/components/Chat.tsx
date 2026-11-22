import DOMPurify from "dompurify";
import { marked } from "marked";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import type { ChatMessage } from "../hooks/useChat";
import type { TraceItem } from "../labels";

/**
 * The model's answer is Markdown. We render it to HTML and then sanitize it with DOMPurify,
 * because model output is untrusted text: without sanitizing, an injected <script> or
 * <img onerror> in an answer would run in the user's browser (XSS).
 */
function renderMarkdown(text: string): string {
  return DOMPurify.sanitize(marked.parse(text, { async: false }));
}

function Message({ m }: { m: ChatMessage }) {
  const cls = `msg ${m.role}${m.streaming ? " streaming" : ""}`;
  if (m.role === "bot") return <div className={cls} dangerouslySetInnerHTML={{ __html: renderMarkdown(m.text) }} />;
  return <div className={cls}>{m.text}</div>; // user/error text: React escapes it
}

export function Messages({ messages }: { messages: ChatMessage[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  }, [messages]);
  return (
    <div ref={ref} className="chat" aria-live="polite">
      {messages.map((m) => (
        <Message key={m.id} m={m} />
      ))}
    </div>
  );
}

export function Trace({ items }: { items: TraceItem[] }) {
  const end = useRef<HTMLLIElement>(null);
  useEffect(() => end.current?.scrollIntoView({ block: "nearest" }), [items]);
  return (
    <details className="trace" open>
      <summary>מה הסוכנים עושים {items.length > 0 && `(${items.length})`}</summary>
      <ol className="trace-list">
        {items.map((t, i) => (
          <li key={i} className={t.tone} ref={i === items.length - 1 ? end : undefined}>
            <span className="tag">{t.tag}</span>
            <span className="txt">{t.text}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}

export function Composer({ busy, onSend, onStop }: { busy: boolean; onSend: (t: string) => void; onStop: () => void }) {
  const [text, setText] = useState("");
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const t = text.trim();
    if (!t || busy) return;
    setText("");
    onSend(t);
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };
  return (
    <form className="composer" onSubmit={submit}>
      <textarea
        rows={2}
        value={text}
        maxLength={2000}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKey}
        placeholder="למשל: מסלול מים קל בגליל העליון לשבת, עם שני ילדים בני 6"
      />
      {busy ? (
        <button type="button" onClick={onStop}>
          עצירה
        </button>
      ) : (
        <button type="submit" disabled={!text.trim()}>
          שליחה
        </button>
      )}
    </form>
  );
}
