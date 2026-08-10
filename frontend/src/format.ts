/**
 * Small display formatters, shared so the app reads the same everywhere.
 *
 * Each of these existed inline at half a dozen call sites, which is fine
 * until you want to change one: a fixed locale, or relative timestamps,
 * would otherwise be an edit per site with no way to find them all.
 */

/** `1 bug` / `2 bugs` — naive `-s` plural, which is all this app needs. */
export function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? '' : 's'}`
}

/** Date only, in the viewer's locale. For anything older than "today". */
export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString()
}

/** Date and time, for rows where the ordering within a day matters. */
export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString()
}
