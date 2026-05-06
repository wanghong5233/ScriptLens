import { AxiosRequestConfig } from 'axios'
import { request } from './request'

export type LlmProviderValue = 'dashscope' | 'openai'

export type LlmModelCatalogItem = {
  provider: LlmProviderValue
  model: string
  label: string
  available: boolean
  status: 'available' | 'unavailable' | 'unknown' | string
  reason?: string | null
  isVision: boolean
  capabilities: string[]
  contextWindow?: number | null
}

export type LlmModelCatalog = {
  preferredProvider: LlmProviderValue
  defaultModel: string
  defaultVisionModel: string
  models: LlmModelCatalogItem[]
  cacheTtlSeconds: number
}

export function fetchLlmModels(options?: AxiosRequestConfig) {
  return request.get<LlmModelCatalog>('config/llm-models', options)
}
