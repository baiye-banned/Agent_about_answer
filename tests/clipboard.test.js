import test from 'node:test'
import assert from 'node:assert/strict'

import { writeClipboardText } from '../src/utils/clipboard.js'

function restoreGlobal(name, value) {
  if (value === undefined) {
    delete globalThis[name]
  } else {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value,
    })
  }
}

function setGlobal(name, value) {
  Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value,
  })
}

test('writeClipboardText falls back to a temporary textarea when Clipboard API is unavailable', async () => {
  const originalNavigator = globalThis.navigator
  const originalDocument = globalThis.document
  const appended = []
  const removed = []
  let selectedValue = ''
  let copied = false

  setGlobal('navigator', {})
  setGlobal('document', {
    createElement(tag) {
      assert.equal(tag, 'textarea')
      return {
        value: '',
        style: {},
        select() {
          selectedValue = this.value
        },
      }
    },
    body: {
      appendChild(element) {
        appended.push(element)
      },
      removeChild(element) {
        removed.push(element)
      },
    },
    execCommand(command) {
      assert.equal(command, 'copy')
      copied = true
      return true
    },
  })

  try {
    const result = await writeClipboardText('hello')

    assert.equal(result, true)
    assert.equal(selectedValue, 'hello')
    assert.equal(copied, true)
    assert.equal(appended.length, 1)
    assert.deepEqual(removed, appended)
  } finally {
    restoreGlobal('navigator', originalNavigator)
    restoreGlobal('document', originalDocument)
  }
})

test('writeClipboardText uses Clipboard API when available', async () => {
  const originalNavigator = globalThis.navigator
  const originalDocument = globalThis.document
  const writes = []

  setGlobal('navigator', {
    clipboard: {
      writeText: async (value) => writes.push(value),
    },
  })
  setGlobal('document', {
    execCommand() {
      throw new Error('fallback should not run')
    },
  })

  try {
    assert.equal(await writeClipboardText(42), true)
    assert.deepEqual(writes, ['42'])
  } finally {
    restoreGlobal('navigator', originalNavigator)
    restoreGlobal('document', originalDocument)
  }
})
