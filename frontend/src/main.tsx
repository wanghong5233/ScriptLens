import 'normalize.css'
import './global.scss'

import { StrictMode, useCallback, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntdApp, ConfigProvider, Spin } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { BrowserRouter } from 'react-router-dom'
import App from './App'

function MountGlobals() {
  window.$app = AntdApp.useApp()

  const [loading, setLoading] = useState(false)
  const [loadingText, setLoadingText] = useState('')
  const loadingCount = useRef(0)

  window.$showLoading = useCallback(({ title }: { title?: string } = {}) => {
    loadingCount.current++
    setLoading(true)
    setLoadingText(title ?? '')
  }, [])
  window.$hideLoading = useCallback(() => {
    loadingCount.current--
    setTimeout(() => {
      if (loadingCount.current <= 0) {
        setLoading(false)
        setLoadingText('')
      }
    }, 100)
  }, [])

  return (
    <Spin
      spinning={loading}
      tip={loadingText}
      fullscreen
      style={{ zIndex: 9999999 }}
    />
  )
}

const root = document.getElementById('root')
if (!root) {
  throw new Error('Root element #root not found in index.html')
}

createRoot(root).render(
  <StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{ cssVar: true, token: { colorPrimary: '#1F70FE' } }}
    >
      <AntdApp>
        <BrowserRouter>
          <App />
          <MountGlobals />
        </BrowserRouter>
      </AntdApp>
    </ConfigProvider>
  </StrictMode>,
)
