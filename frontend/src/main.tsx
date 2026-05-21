import 'normalize.css'
import './global.scss'

import { StrictMode, useCallback, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntdApp, ConfigProvider, Spin } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import {
  GLOBAL_SPINNER_Z_INDEX,
  LOADING_HIDE_DELAY_MS,
} from './constants/numbers'

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
    }, LOADING_HIDE_DELAY_MS)
  }, [])

  return (
    <Spin
      spinning={loading}
      tip={loadingText}
      fullscreen
      style={{ zIndex: GLOBAL_SPINNER_Z_INDEX }}
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
      theme={{
        cssVar: true,
        token: {
          // ScriptLens 莫兰迪暖色系：用户偏年轻，长时间剧本阅读，避免冷蓝程序员风
          colorPrimary: '#E07A8C',
          colorInfo: '#E07A8C',
          colorLink: '#C95A6F',
          colorBgLayout: '#FAF7F4',
          colorBgContainer: '#FFFBF8',
          colorBorder: '#EDD9D2',
          colorBorderSecondary: '#F5E5DF',
          colorText: '#2C2A29',
          colorTextSecondary: '#6E5F58',
          borderRadius: 8,
          // 字体栈源头在 global.scss：避免 token 与 css 双写不同步，这里只写最关键的引用
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei UI", "Microsoft YaHei", "Source Han Sans CN", "Helvetica Neue", Arial, sans-serif',
          // 中文阅读：14 偏紧凑、16 偏松散，15 是经验最优
          fontSize: 15,
          fontSizeSM: 13,
          fontSizeLG: 16,
          fontSizeHeading1: 28,
          fontSizeHeading2: 22,
          fontSizeHeading3: 18,
          fontSizeHeading4: 16,
          lineHeight: 1.65,
        },
      }}
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
