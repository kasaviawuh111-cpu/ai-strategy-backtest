type SameOriginEnvironment = {
  VITE_API_BASE_URL?: string
  VITE_USE_MOCK?: string
}

export function validateSameOriginEnvironment(environment: SameOriginEnvironment): void {
  if (environment.VITE_USE_MOCK !== 'false') {
    throw new Error('Same-origin Live H5 requires VITE_USE_MOCK=false.')
  }
  if (environment.VITE_API_BASE_URL !== '') {
    throw new Error(
      'Same-origin Live H5 requires VITE_API_BASE_URL to be explicitly empty so requests stay on /api.',
    )
  }
}
