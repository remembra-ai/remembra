import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Clipboard API unavailable (insecure origin, old browser): fall back to a hidden textarea.
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    document.body.removeChild(area);
    return ok;
  }
}

/** Copy text to the clipboard with a toast; `copied` is true for 1.6s after. */
export function useCopy(): [(text: string, what?: string) => void, boolean] {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const copy = (text: string, what = 'Copied') => {
    void copyText(text).then((ok) => {
      if (ok) {
        setCopied(true);
        toast.success(what);
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => setCopied(false), 1600);
      } else {
        toast.error('Copy failed. Select the text and copy it manually.');
      }
    });
  };
  return [copy, copied];
}

