import {
  ArrowUpOutlined,
  BarChartOutlined,
  CloseOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  FileOutlined,
  PictureOutlined,
} from '@ant-design/icons'
import { Button, Image, Input, Select, Space, Tooltip } from 'antd'
import type { TextAreaRef } from 'antd/es/input/TextArea'
import classNames from 'classnames'
import {
  ChangeEvent,
  ClipboardEvent,
  PropsWithChildren,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react'
import './index.scss'
import Recorder from './recorder'
import Uploader from './uploader'

export default function ComSender(
  props: PropsWithChildren<{
    className?: string
    loading?: boolean
    onSend?: (value: string) => void | Promise<void>
    onAbort?: () => void
    onContract?: () => void
    sessionId?: string
    knowledgeControl?: {
      usingUser: boolean
      selectValue?: number
      options: { value: number; label: string; disabled?: boolean }[]
      showSelect: boolean
      selectWidth?: string | number
      loadingUser?: boolean
      disableUserToggle?: boolean
      disableSelect?: boolean
      onToggleUser: (checked: boolean) => void
      onSelectUserKb: (value: number) => void
    }
    ragModeControl?: {
      value: 'fast' | 'deep'
      loading?: boolean
      disabled?: boolean
      width?: string | number
      onChange: (value: 'fast' | 'deep') => void
    }
    researchModeControl?: {
      enabled: boolean
      disabled?: boolean
      onToggle: (enabled: boolean) => void
      preset?: 'quick' | 'medium' | 'deep'
      presetDisabled?: boolean
      presetWidth?: string | number
      onPresetChange?: (value: 'quick' | 'medium' | 'deep') => void
    }
    modelControl?: {
      value: string
      options: { value: string; label: string; disabled?: boolean }[]
      loading?: boolean
      disabled?: boolean
      width?: string | number
      onChange: (value: string) => void
    }
    systemStatusControl?: {
      title?: string
      onClick: () => void
      disabled?: boolean
    }
    onAttachmentsChange?: (files: API.ChatAttachment[]) => void
    pendingAttachments?: API.ChatAttachment[]
    onRemovePendingAttachment?: (id: number) => void
    onFileSelected?: (file: File) => void
    imageAttachments?: API.ChatImageAttachment[]
    imageProcessing?: boolean
    onImageFilesSelected?: (files: File[]) => void | Promise<void>
    onRemoveImageAttachment?: (id: string) => void
    disableImageUpload?: boolean
    value?: string
    onValueChange?: (value: string) => void
    focusKey?: number
  }>,
) {
  const {
    className,
    onSend,
    onAbort,
    onContract,
    loading,
    sessionId,
    knowledgeControl,
    ragModeControl,
    researchModeControl,
    modelControl,
    systemStatusControl,
    onAttachmentsChange,
    pendingAttachments = [],
    onRemovePendingAttachment,
    onFileSelected,
    imageAttachments = [],
    onImageFilesSelected,
    onRemoveImageAttachment,
    imageProcessing = false,
    disableImageUpload,
    value: controlledValue,
    onValueChange,
    focusKey,
    ...rest
  } = props
  const [innerValue, setInnerValue] = useState('')
  const textareaRef = useRef<TextAreaRef>(null)
  const imageInputRef = useRef<HTMLInputElement>(null)
  const isControlled = typeof controlledValue === 'string'
  const value = isControlled ? controlledValue! : innerValue
  // 保留向后兼容的 props，避免升级期间调用方报错
  void onContract
  void onAttachmentsChange
  void sessionId

  useEffect(() => {
    if (typeof focusKey === 'number' && textareaRef.current) {
      textareaRef.current.focus()
    }
  }, [focusKey])

  const updateValue = (next: string) => {
    if (onValueChange) onValueChange(next)
    if (!isControlled) setInnerValue(next)
  }

  const send = useCallback(async () => {
    if (loading) return
    if (!value?.trim() && imageAttachments.length === 0) return
    await onSend?.(value)
    updateValue('')
  }, [imageAttachments.length, loading, onSend, value])

  const handleInputPaste = useCallback(
    async (event: ClipboardEvent<HTMLTextAreaElement>) => {
      if (!onImageFilesSelected) return
      const items = Array.from(event.clipboardData?.items || [])
      const imageFiles: File[] = []
      for (const item of items) {
        if (item.type.startsWith('image/')) {
          const file = item.getAsFile()
          if (file) imageFiles.push(file)
        }
      }
      if (!imageFiles.length) return
      event.preventDefault()
      await onImageFilesSelected(imageFiles)
    },
    [onImageFilesSelected],
  )

  const handleImageInputChange = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files || [])
      event.target.value = ''
      if (!files.length || !onImageFilesSelected) return
      await onImageFilesSelected(files)
    },
    [onImageFilesSelected],
  )

  function handleSendClick() {
    if (loading && onAbort) {
      onAbort()
    } else {
      send()
    }
  }
  return (
    <div className={classNames('com-sender', className)} {...rest}>
      {pendingAttachments.length > 0 && (
        <div className="com-sender__pending-attachments">
          {pendingAttachments.map((att) => (
            <div key={att.id} className="com-sender__pending-chip">
              <FileOutlined style={{ fontSize: 12, color: '#0862fe' }} />
              <Tooltip title={att.title}>
                <span className="com-sender__pending-name">{att.title}</span>
              </Tooltip>
              <Button
                type="text"
                size="small"
                icon={<CloseOutlined style={{ fontSize: 10 }} />}
                className="com-sender__pending-remove"
                onClick={() => onRemovePendingAttachment?.(att.id)}
              />
            </div>
          ))}
        </div>
      )}
      {imageAttachments.length > 0 && (
        <div className="com-sender__image-attachments">
          <Space wrap size={[8, 8]}>
            {imageAttachments.map((item) => (
              <span key={item.id} className="com-sender__image-chip">
                <Image
                  src={item.dataUrl}
                  alt={item.name}
                  width={36}
                  height={36}
                  className="com-sender__image-chip-thumb"
                  preview={{ mask: false }}
                />
                <button
                  type="button"
                  className="com-sender__image-chip-remove"
                  onClick={(e) => {
                    e.stopPropagation()
                    e.preventDefault()
                    onRemoveImageAttachment?.(item.id)
                  }}
                  title="移除图片"
                >
                  ×
                </button>
              </span>
            ))}
          </Space>
        </div>
      )}
      <Input.TextArea
        ref={textareaRef}
        value={value}
        onChange={(e) => updateValue(e.target.value)}
        onPaste={handleInputPaste}
        placeholder="输入你的问题… Ctrl+V 粘贴图片（最多 4 张）"
        autoSize={{ minRows: 2 }}
        autoFocus
      />

      <div className="com-sender__actions">
        <Space className="com-sender__actions-left" size={12}>
          {modelControl ? (
            <Tooltip title="模型切换">
              <Select
                size="small"
                className="com-sender__model-select"
                value={modelControl.value}
                options={modelControl.options}
                loading={modelControl.loading}
                disabled={modelControl.disabled}
                style={modelControl.width ? { width: modelControl.width } : undefined}
                onChange={(value) => modelControl.onChange(String(value))}
                optionFilterProp="label"
                popupMatchSelectWidth={false}
                showSearch
              />
            </Tooltip>
          ) : null}
        </Space>

        <Space className="com-sender__actions-right" size={4}>
          {researchModeControl ? (
            <>
              {researchModeControl.enabled && researchModeControl.onPresetChange ? (
                <Tooltip title="深度研究档位">
                  <Select
                    size="small"
                    className="com-sender__research-preset-select"
                    value={researchModeControl.preset || 'medium'}
                    disabled={researchModeControl.presetDisabled}
                    style={
                      researchModeControl.presetWidth
                        ? { width: researchModeControl.presetWidth }
                        : undefined
                    }
                    options={[
                      { label: '快速', value: 'quick' },
                      { label: '标准', value: 'medium' },
                      { label: '深度', value: 'deep' },
                    ]}
                    onChange={(value) =>
                      researchModeControl.onPresetChange?.(
                        value as 'quick' | 'medium' | 'deep',
                      )
                    }
                    popupMatchSelectWidth={false}
                  />
                </Tooltip>
              ) : null}
              <Tooltip
                title={researchModeControl.enabled ? '关闭深度研究工具' : '开启深度研究工具'}
              >
                <Button
                  type="text"
                  className={classNames('com-sender__toolbar-icon-btn', {
                    'com-sender__toolbar-icon-btn--active': researchModeControl.enabled,
                  })}
                  icon={<ExperimentOutlined />}
                  disabled={researchModeControl.disabled}
                  onClick={() =>
                    researchModeControl.onToggle(!researchModeControl.enabled)
                  }
                />
              </Tooltip>
            </>
          ) : null}
          {knowledgeControl ? (
            <Tooltip title={knowledgeControl.usingUser ? '关闭 RAG 检索' : '开启 RAG 检索'}>
              <Button
                type="text"
                className={classNames('com-sender__toolbar-icon-btn', {
                  'com-sender__toolbar-icon-btn--active': knowledgeControl.usingUser,
                })}
                icon={<DatabaseOutlined />}
                loading={knowledgeControl.loadingUser}
                disabled={knowledgeControl.disableUserToggle}
                onClick={() => knowledgeControl.onToggleUser(!knowledgeControl.usingUser)}
              />
            </Tooltip>
          ) : null}
          {ragModeControl ? (
            <Tooltip title="RAG 检索模式">
              <Select
                size="small"
                className="com-sender__rag-select"
                value={ragModeControl.value}
                disabled={ragModeControl.disabled}
                loading={ragModeControl.loading}
                style={ragModeControl.width ? { width: ragModeControl.width } : undefined}
                options={[
                  { label: '快速', value: 'fast' },
                  { label: '深度', value: 'deep' },
                ]}
                onChange={(value) => ragModeControl.onChange(value as 'fast' | 'deep')}
                popupMatchSelectWidth={false}
              />
            </Tooltip>
          ) : null}
          {knowledgeControl?.showSelect ? (
            <Tooltip title="选择知识库">
              <Select
                size="small"
                className="com-sender__kb-select com-sender__kb-select--inline"
                value={knowledgeControl.selectValue}
                options={knowledgeControl.options}
                placeholder="知识库"
                disabled={knowledgeControl.disableSelect}
                style={
                  knowledgeControl.selectWidth
                    ? { width: knowledgeControl.selectWidth }
                    : undefined
                }
                onChange={(value) =>
                  knowledgeControl.onSelectUserKb(Number(value))
                }
                popupMatchSelectWidth={false}
                showSearch
                optionFilterProp="label"
              />
            </Tooltip>
          ) : null}
          <Button
            className="com-sender__action--document"
            type="default"
            shape="circle"
            icon={<PictureOutlined />}
            onClick={() => imageInputRef.current?.click()}
            loading={imageProcessing}
            disabled={disableImageUpload}
            title="添加图片"
          />
          <input
            ref={imageInputRef}
            type="file"
            accept="image/*"
            multiple
            style={{ display: 'none' }}
            onChange={handleImageInputChange}
          />
          {onFileSelected ? (
            <Uploader onFileSelected={(file) => onFileSelected?.(file)} />
          ) : null}
          {systemStatusControl ? (
            <Tooltip title={systemStatusControl.title || '系统状态'}>
              <Button
                type="text"
                className="com-sender__toolbar-icon-btn"
                icon={<BarChartOutlined />}
                onClick={systemStatusControl.onClick}
                disabled={systemStatusControl.disabled}
              />
            </Tooltip>
          ) : null}
          <Tooltip title="语音输入">
            <Recorder
              buttonClassName="com-sender__action--voice"
              activeButtonClassName="com-sender__action--voice--recording"
              onMessage={(text) => {
                updateValue(text)
              }}
            />
          </Tooltip>
          <Button
            className="com-sender__action--send"
            type="primary"
            shape="circle"
            onClick={handleSendClick}
            disabled={!loading && !value?.trim() && imageAttachments.length === 0}
          >
            {loading ? <span className="com-sender__send-stop-icon" /> : <ArrowUpOutlined />}
          </Button>
        </Space>
      </div>
    </div>
  )
}
