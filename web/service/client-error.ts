import { isAbortLikeError } from './abort-error'

// Prism: also treat "signal is aborted" message-only errors as aborts.
export function reportClientError(error: unknown) {
  if (!isAbortLikeError(error)) console.error(error)
}
