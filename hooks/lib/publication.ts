// A module-local critical section for session changes and snapshot publication.
// No host calls here: a reload destroys both the queue and its in-flight work.
let tail = Promise.resolve()
let generation = 0

/** Serializes the short state writes that publish or invalidate a snapshot. */
export async function lockPublication(): Promise<() => void> {
  const previous = tail
  let release: () => void = () => {}
  tail = new Promise<void>(resolve => { release = resolve })
  await previous
  return release
}

/** The displayed session's generation; captured before a bridge computation. */
export function publicationGeneration(): number {
  return generation
}

/** Invalidates work for the preceding session (while holding the lock). */
export function invalidatePublication(): void {
  generation++
}
