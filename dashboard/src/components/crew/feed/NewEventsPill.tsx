// "3 new events" over the top of the list while the viewer reads older rows.
// The list keeps its place as events arrive; the pill takes them back up.

import { AnimatePresence, motion } from 'framer-motion';
import { ArrowUp } from 'lucide-react';
import { useCrewMotion } from '../../../lib/motion';

export function NewEventsPill({ count, onClick }: { count: number; onClick: () => void }) {
  const crewMotion = useCrewMotion();
  return (
    <div className="pointer-events-none sticky top-2 z-10 flex h-0 justify-center">
      <AnimatePresence>
        {count > 0 && (
          <motion.button
            key="pill"
            type="button"
            variants={crewMotion.pill}
            initial="initial"
            animate="animate"
            exit="exit"
            onClick={onClick}
            className="rr-btn-primary pointer-events-auto inline-flex items-center gap-1.5 px-3 py-1.5 font-mono text-[12px] shadow-[var(--shadow)]"
          >
            <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" />
            {count} new event{count === 1 ? '' : 's'}
          </motion.button>
        )}
      </AnimatePresence>
    </div>
  );
}
