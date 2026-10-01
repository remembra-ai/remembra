// Model-written text on the page: the parts from inlineParts, as React
// elements and text nodes only. Nothing here is ever parsed as HTML.

import { Fragment } from 'react';
import { inlineParts } from '../../lib/marshalDesk';

export function InlineText({ text }: { text: string }) {
  return (
    <>
      {inlineParts(text).map((part, index) => {
        switch (part.kind) {
          case 'code':
            return (
              <code key={index} className="rounded-[2px] bg-paper-2 px-1 font-mono text-[0.86em] text-ink">
                {part.text}
              </code>
            );
          case 'bold':
            return (
              <strong key={index} className="font-semibold">
                {part.text}
              </strong>
            );
          case 'link':
            return (
              <a
                key={index}
                href={part.href}
                target="_blank"
                rel="noopener noreferrer"
                className="text-ink underline decoration-rule underline-offset-2 [overflow-wrap:anywhere] hover:decoration-signal"
              >
                {part.text}
              </a>
            );
          default:
            return <Fragment key={index}>{part.text}</Fragment>;
        }
      })}
    </>
  );
}
