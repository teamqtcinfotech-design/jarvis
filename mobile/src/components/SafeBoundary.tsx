import React from 'react';

import * as diag from '@/lib/diag';

interface Props {
  /** What is inside, for the diagnostics line ("card", "screen"). */
  name: string;
  /** Shown instead when the inside fails to draw. */
  fallback?: React.ReactNode;
  children: React.ReactNode;
}

/**
 * A part of the screen that failed to draw is replaced by its fallback, instead of taking the whole app down (in a
 * release build an error while drawing closes the app, and Android may bring it back on a blank screen). The
 * error is noted for "Send diagnostics". Give it a key that changes with its content so the next content is tried.
 */
export class SafeBoundary extends React.Component<Props, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    diag.note('error', `render ${this.props.name}`, String((error as Error)?.message ?? error).slice(0, 200));
  }

  render() {
    return this.state.failed ? (this.props.fallback ?? null) : this.props.children;
  }
}
