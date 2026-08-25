/**
 * A boundary around one view, one chart, one anything.
 *
 * The gallery renders nine views it does not own, each written by someone
 * else, several still in flight. Without this, the first view that throws
 * blanks the page and takes the other eight with it — which is the single
 * worst failure mode for a surface whose entire job is comparing them.
 *
 * It also stands in for the degrade-gracefully requirement: a signature whose
 * lazily-loaded Three.js scene fails must not take its charts down with it,
 * and the only way to see that is to render the signature and the charts
 * inside separate boundaries and check the charts survive.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  /** Named in the fallback, so a thrown error says WHICH thing threw. */
  label: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // The gallery is a dev surface; the console is where the stack is useful.
    console.error(`[gallery] ${this.props.label} threw`, error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (error !== null) {
      return (
        <div className="rounded-md border border-bad bg-bad-soft p-2 text-[0.68rem] text-bad">
          <div className="font-bold">{this.props.label} threw</div>
          <div className="mt-1 font-mono break-words">{error.message}</div>
        </div>
      );
    }
    return this.props.children;
  }
}
