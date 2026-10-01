import { NODE_ROW } from './rows';

/** A question on the trail: a hollow square node, the question in mono after `?`. */
export function QuestionEntry({ question }: { question: string }) {
  return (
    <div className={NODE_ROW}>
      <span aria-hidden="true" className="relative z-[1] mt-[5px] h-2 w-2 justify-self-center border-2 border-ink-2 bg-panel" />
      <p className="min-w-0 font-mono text-[13px] leading-[18px] text-ink-2 [overflow-wrap:anywhere]">
        <span aria-hidden="true">? </span>
        <span className="sr-only">Asked: </span>
        {question}
      </p>
    </div>
  );
}
