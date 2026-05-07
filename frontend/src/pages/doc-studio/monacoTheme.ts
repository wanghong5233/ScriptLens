/**
 * ScriptLens Monaco 主题：莫兰迪暖色浅色（米白底 / 近黑文字 / 柔和珊瑚强调）。
 *
 * 选色第一性原理：
 *   - 用户群体偏年轻女性（短剧选品 / 编剧 / 审核），冷蓝程序员主题不契合
 *   - 剧本是「长时间阅读 + 选区改写」高频场景，纯白背景刺眼、纯黑文字疲劳
 *   - 对标 Notion / 飞书文档 / Bear，背景用极淡暖白，正文用近黑暖灰，强调用珊瑚
 *
 * 仅注册一次，副作用导入：在 doc-studio/index.tsx 顶部 `import './monacoTheme'`
 */
import { loader } from '@monaco-editor/react'
import type * as Monaco from 'monaco-editor'

export const SCRIPTLENS_LIGHT_THEME = 'scriptlens-light'

let registered = false

export function registerScriptLensMonacoTheme(): void {
  if (registered) return
  registered = true
  void loader.init().then((monaco: typeof Monaco) => {
    monaco.editor.defineTheme(SCRIPTLENS_LIGHT_THEME, {
      base: 'vs',
      inherit: true,
      rules: [
        { token: '', foreground: '2C2A29' },
        { token: 'comment', foreground: 'B89A92', fontStyle: 'italic' },
        { token: 'keyword', foreground: 'C95A6F' },
        { token: 'string', foreground: '8C6C56' },
        { token: 'number', foreground: 'B07A4A' },
      ],
      colors: {
        'editor.background': '#FFFBF8',
        'editor.foreground': '#2C2A29',
        'editor.lineHighlightBackground': '#FBF1ED',
        'editor.lineHighlightBorder': '#FBF1ED',
        'editor.selectionBackground': '#F8D7D0',
        'editor.inactiveSelectionBackground': '#FBE8E3',
        'editor.findMatchBackground': '#FFD7C7',
        'editor.findMatchHighlightBackground': '#FFEDE6',
        'editorCursor.foreground': '#E07A8C',
        'editorLineNumber.foreground': '#BDA7A0',
        'editorLineNumber.activeForeground': '#E07A8C',
        'editorIndentGuide.background': '#F2E1DA',
        'editorIndentGuide.activeBackground': '#E8C8BE',
        'editorWhitespace.foreground': '#F2E1DA',
        'editorWidget.background': '#FFFBF8',
        'editorWidget.border': '#EDD9D2',
        'editorSuggestWidget.background': '#FFFBF8',
        'editorSuggestWidget.border': '#EDD9D2',
        'editorSuggestWidget.selectedBackground': '#FBE8E3',
        'scrollbarSlider.background': '#EDD9D266',
        'scrollbarSlider.hoverBackground': '#E8C8BE99',
        'scrollbarSlider.activeBackground': '#E07A8C66',
      },
    })
  })
}

registerScriptLensMonacoTheme()
