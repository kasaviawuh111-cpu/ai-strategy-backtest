type PublicLiveEnvironment = {
  VITE_API_BASE_URL?: string
  VITE_USE_MOCK?: string
}

const LOOPBACK_OR_PRIVATE = /^(?:localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|\[?::1\]?)$/i

export function validatePublicLiveEnvironment(environment: PublicLiveEnvironment): string {
  if (environment.VITE_USE_MOCK !== 'false') {
    throw new Error('Public Live H5 requires VITE_USE_MOCK=false.')
  }

  const rawBaseUrl = environment.VITE_API_BASE_URL?.trim()
  if (!rawBaseUrl) {
    throw new Error('Public Live H5 requires a non-empty VITE_API_BASE_URL.')
  }

  let apiUrl: URL
  try {
    apiUrl = new URL(rawBaseUrl)
  } catch {
    throw new Error('Public Live H5 requires VITE_API_BASE_URL to be a valid HTTPS origin.')
  }

  if (
    apiUrl.protocol !== 'https:'
    || apiUrl.username
    || apiUrl.password
    || apiUrl.pathname !== '/'
    || apiUrl.search
    || apiUrl.hash
    || LOOPBACK_OR_PRIVATE.test(apiUrl.hostname)
    || apiUrl.hostname.endsWith('.local')
  ) {
    throw new Error(
      'Public Live H5 requires VITE_API_BASE_URL to be a public HTTPS origin without credentials, path, query or hash.',
    )
  }

  return apiUrl.origin
}
