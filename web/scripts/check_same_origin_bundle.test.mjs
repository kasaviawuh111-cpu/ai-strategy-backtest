import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  FORBIDDEN_SAME_ORIGIN_MARKERS,
  assertSameOriginJavaScript,
  checkSameOriginBundle,
} from './check_same_origin_bundle.mjs'

describe('same-origin bundle verifier', () => {
  const temporaryDirectories = []

  afterEach(async () => {
    vi.unstubAllEnvs()
    await Promise.all(temporaryDirectories.splice(0).map((directory) =>
      rm(directory, { force: true, recursive: true }),
    ))
  })

  it('accepts normal interface text and relative API requests', () => {
    expect(() => assertSameOriginJavaScript(
      'fetch("/api/v1/strategy-drafts");document.title="A 股策略回测";',
    )).not.toThrow()
  })

  it.each(FORBIDDEN_SAME_ORIGIN_MARKERS)('rejects forbidden marker %s', (marker) => {
    expect(() => assertSameOriginJavaScript(`const leaked=${JSON.stringify(marker)}`))
      .toThrow(`forbidden marker: ${marker}`)
  })

  it('scans the generated JavaScript assets under an explicit Live environment', async () => {
    const distDirectory = await mkdtemp(resolve(tmpdir(), 'same-origin-bundle-'))
    temporaryDirectories.push(distDirectory)
    await mkdir(resolve(distDirectory, 'assets'))
    await writeFile(
      resolve(distDirectory, 'assets', 'index.js'),
      'fetch("/api/v1/strategy-drafts")',
      'utf8',
    )
    vi.stubEnv('VITE_USE_MOCK', 'false')
    vi.stubEnv('VITE_API_BASE_URL', '')

    await expect(checkSameOriginBundle(distDirectory)).resolves.toBe(1)
  })

  it('rejects bundle verification when the environment is not explicitly same-origin', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.cn')

    await expect(checkSameOriginBundle('/unused')).rejects
      .toThrow('explicitly empty VITE_API_BASE_URL')
  })
})
