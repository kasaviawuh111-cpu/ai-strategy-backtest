export const LIVE_API_PROXY_TARGET = 'http://127.0.0.1:8011'

type LiveEnvironment = {
  VITE_API_BASE_URL?: string
  VITE_API_PROXY_TARGET?: string
  VITE_USE_MOCK?: string
}

export function validateLiveEnvironment(environment: LiveEnvironment): string {
  if (environment.VITE_API_PROXY_TARGET !== LIVE_API_PROXY_TARGET) {
    throw new Error(
      `Live H5 requires VITE_API_PROXY_TARGET=${LIVE_API_PROXY_TARGET}; ` +
        'missing, remote, HTTPS, localhost aliases, other ports, paths and trailing slashes are rejected.',
    )
  }
  if (environment.VITE_USE_MOCK !== 'false') {
    throw new Error('Live H5 requires VITE_USE_MOCK=false.')
  }
  if ((environment.VITE_API_BASE_URL ?? '') !== '') {
    throw new Error('Live H5 requires an empty VITE_API_BASE_URL so all API calls use the isolated proxy.')
  }
  return environment.VITE_API_PROXY_TARGET
}
