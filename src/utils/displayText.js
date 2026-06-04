export function displayText(value) {
  if (typeof value !== 'object' || value === null) return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}
