import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Toaster } from 'sonner'
import './index.css'
import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
      <Toaster
        position="bottom-right"
        offset={{ bottom: 24 }}
        mobileOffset={{ bottom: 'calc(84px + env(safe-area-inset-bottom))' }}
        toastOptions={{
          style: {
            background: 'var(--panel)',
            border: '1px solid var(--rule-strong)',
            color: 'var(--ink)',
            borderRadius: '3px',
            fontFamily: 'var(--f-body)',
          },
        }}
        closeButton
      />
    </ErrorBoundary>
  </StrictMode>,
)
