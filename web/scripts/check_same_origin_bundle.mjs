import { readFile, readdir } from 'node:fs/promises'
import { extname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

export const LEGACY_PUBLIC_API_ORIGIN =
  'https://ashare-backtest-api-305722-11-1330091763.sh.run.tcloudbase.com'

export const FORBIDDEN_SAME_ORIGIN_MARKERS = [
  'draft_mock_demo_001',
  'mock_demo',
  LEGACY_PUBLIC_API_ORIGIN,
  '2026-08-06',
  '演示',
  '真实数据',
  '正式数据',
  '真实接口',
  '真实回测',
]

export function assertSameOriginJavaScript(javascript) {
  for (const marker of FORBIDDEN_SAME_ORIGIN_MARKERS) {
    if (javascript.includes(marker)) {
      throw new Error(`Same-origin Live bundle contains forbidden marker: ${marker}`)
    }
  }
}

async function listJavaScriptFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  const nested = await Promise.all(entries.map(async (entry) => {
    const path = resolve(directory, entry.name)
    if (entry.isDirectory()) return listJavaScriptFiles(path)
    return entry.isFile() && extname(entry.name) === '.js' ? [path] : []
  }))
  return nested.flat()
}

export async function checkSameOriginBundle(distDirectory = resolve('dist')) {
  if (process.env.VITE_USE_MOCK !== 'false') {
    throw new Error('Same-origin bundle verification requires VITE_USE_MOCK=false.')
  }
  if (process.env.VITE_API_BASE_URL !== '') {
    throw new Error('Same-origin bundle verification requires an explicitly empty VITE_API_BASE_URL.')
  }
  if (process.env.VITE_DATA_AS_OF_DATE !== '') {
    throw new Error(
      'Same-origin Live bundle requires an empty VITE_DATA_AS_OF_DATE (no frozen cutoff).',
    )
  }

  const javascriptFiles = await listJavaScriptFiles(resolve(distDirectory, 'assets'))
  if (javascriptFiles.length === 0) {
    throw new Error('Same-origin Live bundle contains no JavaScript asset.')
  }

  const javascript = (await Promise.all(
    javascriptFiles.map((path) => readFile(path, 'utf8')),
  )).join('\n')
  assertSameOriginJavaScript(javascript)
  return javascriptFiles.length
}

const invokedUrl = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : undefined
if (invokedUrl === import.meta.url) {
  const javascriptCount = await checkSameOriginBundle()
  console.log(
    `verified same-origin Live bundle: ${javascriptCount} JS asset(s), relative /api, no forbidden identity markers`,
  )
}
