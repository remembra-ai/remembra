import { DESK_COPY, footerText, type UsageEvent } from '../../lib/marshalDesk';

/** `gpt-4o-mini · 2 reads · <$0.001 · not billed to your credits`, and the redaction line when a key was removed. */
export function UsageFooter({ usage }: { usage: UsageEvent }) {
  return (
    <div className="font-mono text-[11px] leading-relaxed text-ink-3">
      <p>{footerText(usage)}</p>
      {usage.input_redactions > 0 && (
        <p>
          <span aria-hidden="true">› </span>
          {DESK_COPY.redacted}
        </p>
      )}
    </div>
  );
}
