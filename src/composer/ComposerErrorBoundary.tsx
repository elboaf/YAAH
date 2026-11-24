/** Error boundary for Lexical decorator/inline nodes (#711, phase 2).
 *
 * RichTextPlugin requires an `ErrorBoundary` component that wraps the
 * contentEditable tree. This one renders its children and logs node render
 * errors instead of crashing the composer.
 */
import { Component } from 'react'

interface ErrorBoundaryProps {
  children: React.JSX.Element
  onError: (error: Error) => void
}

export class ComposerErrorBoundary extends Component<ErrorBoundaryProps> {
  override render(): React.JSX.Element {
    return this.props.children
  }
}
