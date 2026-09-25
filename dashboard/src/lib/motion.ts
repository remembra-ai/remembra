/**
 * Framer Motion animation presets for Remembra Dashboard
 * Premium, subtle, performant animations inspired by Linear & Vercel
 */

import type { Variants, Transition } from 'framer-motion';

// ─── Spring Presets ─────────────────────────────────────────────
export const spring = {
  snappy: { type: 'spring', stiffness: 500, damping: 30 } as Transition,
  smooth: { type: 'spring', stiffness: 300, damping: 25 } as Transition,
  gentle: { type: 'spring', stiffness: 200, damping: 20 } as Transition,
  bouncy: { type: 'spring', stiffness: 400, damping: 15 } as Transition,
};

// ─── Page Transition ────────────────────────────────────────────
export const pageTransition: Variants = {
  initial: {
    opacity: 0,
    y: 12,
    scale: 0.99,
    filter: 'blur(4px)',
  },
  animate: {
    opacity: 1,
    y: 0,
    scale: 1,
    filter: 'blur(0px)',
    transition: {
      duration: 0.3,
      ease: [0.22, 1, 0.36, 1], // easeOutQuint
    },
  },
  exit: {
    opacity: 0,
    y: -8,
    scale: 0.995,
    filter: 'blur(2px)',
    transition: {
      duration: 0.15,
      ease: [0.4, 0, 1, 1], // easeIn
    },
  },
};

// ─── Stagger Container ─────────────────────────────────────────
export const staggerContainer: Variants = {
  initial: {},
  animate: {
    transition: {
      staggerChildren: 0.06,
      delayChildren: 0.1,
    },
  },
};

// ─── Stagger Items ──────────────────────────────────────────────
export const staggerItem: Variants = {
  initial: { opacity: 0, y: 16 },
  animate: {
    opacity: 1,
    y: 0,
    transition: {
      duration: 0.4,
      ease: [0.22, 1, 0.36, 1],
    },
  },
};

// ─── Fade In ────────────────────────────────────────────────────
export const fadeIn: Variants = {
  initial: { opacity: 0 },
  animate: {
    opacity: 1,
    transition: { duration: 0.3, ease: 'easeOut' },
  },
  exit: {
    opacity: 0,
    transition: { duration: 0.15 },
  },
};

// ─── Scale In (for modals, cards) ───────────────────────────────
export const scaleIn: Variants = {
  initial: { opacity: 0, scale: 0.95 },
  animate: {
    opacity: 1,
    scale: 1,
    transition: {
      duration: 0.2,
      ease: [0.22, 1, 0.36, 1],
    },
  },
  exit: {
    opacity: 0,
    scale: 0.98,
    transition: { duration: 0.15 },
  },
};

// ─── Slide In (for sidebars, panels) ────────────────────────────
export const slideInLeft: Variants = {
  initial: { opacity: 0, x: -20 },
  animate: {
    opacity: 1,
    x: 0,
    transition: { duration: 0.3, ease: [0.22, 1, 0.36, 1] },
  },
  exit: {
    opacity: 0,
    x: -20,
    transition: { duration: 0.2 },
  },
};

export const slideInRight: Variants = {
  initial: { opacity: 0, x: 20 },
  animate: {
    opacity: 1,
    x: 0,
    transition: { duration: 0.3, ease: [0.22, 1, 0.36, 1] },
  },
  exit: {
    opacity: 0,
    x: 20,
    transition: { duration: 0.2 },
  },
};

// ─── Card Hover (interactive cards) ─────────────────────────────
export const cardHover = {
  rest: {
    y: 0,
    boxShadow: '0 0 0 rgba(255, 91, 20, 0)',
  },
  hover: {
    y: -2,
    boxShadow: '0 8px 30px rgba(255, 91, 20, 0.12)',
    transition: spring.snappy,
  },
  tap: {
    y: 0,
    scale: 0.995,
    transition: { duration: 0.1 },
  },
};

// ─── Pulse Glow (for active/live indicators) ────────────────────
export const pulseGlow: Variants = {
  animate: {
    boxShadow: [
      '0 0 0 0 rgba(255, 91, 20, 0.4)',
      '0 0 0 8px rgba(255, 91, 20, 0)',
    ],
    transition: {
      duration: 2,
      repeat: Infinity,
      ease: 'easeInOut',
    },
  },
};

// ─── Number Count Up ────────────────────────────────────────────
export const countUpTransition: Transition = {
  duration: 0.8,
  ease: [0.22, 1, 0.36, 1],
};
