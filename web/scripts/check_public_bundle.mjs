import { readFile, readdir } from 'node:fs/promises'
import { resolve } from 'node:path'

const apiBaseUrl = process.env.VITE_API_BASE_URL?.trim()
if (!apiBaseUrl) throw new Error('VITE_API_BASE_URL is required to verify the public Live bundle.')

const expectedOrigin = new URL(apiBaseUrl).origin
const assetsDirectory = resolve('dist/assets')
const assetNames = await readdir(assetsDirectory)
const javascriptNames = assetNames.filter((name) => name.endsWith('.js'))
if (javascriptNames.length === 0) throw new Error('Public Live bundle contains no JavaScript asset.')

const javascript = (await Promise.all(
  javascriptNames.map((name) => readFile(resolve(assetsDirectory, name), 'utf8')),
)).join('\n')

const requiredMarkers = [expectedOrigin, '真实接口']
for (const marker of requiredMarkers) {
  if (!javascript.includes(marker)) {
    throw new Error(`Public Live bundle is missing required marker: ${marker}`)
  }
}

const forbiddenMockMarkers = [
  'mock_demo',
  'draft_mock_demo_001',
  '演示数据：收益、交易与事件都是固定样例',
  '界面预览',
]
for (const marker of forbiddenMockMarkers) {
  if (javascript.includes(marker)) {
    throw new Error(`Public Live bundle still contains a Mock execution marker: ${marker}`)
  }
}

console.log(
  `verified public Live bundle: ${javascriptNames.length} JS asset(s), API ${expectedOrigin}, no Mock execution markers`,
)
