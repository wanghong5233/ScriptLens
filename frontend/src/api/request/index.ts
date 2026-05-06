import { createRequest } from './request'
import { getApiBase } from '../env'

export const request = createRequest({
  baseURL: getApiBase(),
  loading: true,
  errorToast: true,
  cancelRepeat: true,
  unwrap: true,
})
