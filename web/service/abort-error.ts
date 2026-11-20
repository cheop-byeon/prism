// Prism: aborted requests (navigation, unmounts, query cancellation) are expected;
// keep them out of the console error log.
const abortErrorMessages = ['aborted', 'signal is aborted without reason']

export const isAbortLikeError = (error: unknown): boolean => {
  if (!error) return false

  if (typeof error === 'object') {
    const maybeError = error as {
      name?: unknown
      message?: unknown
      cause?: unknown
    }

    if (maybeError.name === 'AbortError') return true

    if (typeof maybeError.message === 'string') {
      const normalizedMessage = maybeError.message.toLowerCase()
      if (abortErrorMessages.some((message) => normalizedMessage.includes(message))) return true
    }

    return isAbortLikeError(maybeError.cause)
  }

  if (typeof error === 'string') {
    const normalizedError = error.toLowerCase()
    return abortErrorMessages.some((message) => normalizedError.includes(message))
  }

  return false
}
