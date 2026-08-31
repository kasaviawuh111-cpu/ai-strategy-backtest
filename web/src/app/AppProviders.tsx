import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Component, type ErrorInfo, type ReactNode } from 'react'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 15_000 },
    mutations: { retry: 0 },
  },
})

class AppErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Application render failed', error, info.componentStack)
  }

  render() {
    if (this.state.failed) {
      return (
        <main className="fatal-error">
          <span>页面没有完整加载</span>
          <h1>策略内容还在，刷新页面即可重试。</h1>
          <button type="button" onClick={() => window.location.reload()}>刷新页面</button>
        </main>
      )
    }
    return this.props.children
  }
}

export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <AppErrorBoundary>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </AppErrorBoundary>
  )
}
