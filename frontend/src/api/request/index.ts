import { createRequest } from './request'
import { getApiBase } from '../env'

// loading: false ——
// ScriptLens 里几乎所有交互都用 antd `message` / 组件局部 Spin / 报告进度面板表达进行中状态，
// 没有任何代码真正期望「全屏 fullscreen 遮罩」。但报告生成阶段会同时跑两条轮询：
//   - 1s 轮询 GET /scripts/{id}/progress
//   - 3s 轮询 GET /scripts/{id} + GET /scripts/{id}/view
// 一旦默认 loading: true，每个请求都会触发 main.tsx 里那个 fullscreen `<Spin>` 的
// show()→hide()，中间 Monaco 编辑器整块灰幕+"加载中..."不停闪，体验灾难。
//
// 所以改为：默认不显示全屏 mask；少数真正需要阻挡 UI 的地方（若有）显式传 `loading: true` opt-in。
// `$showLoading` / `$hideLoading` 与 loadingPlugin 都保留，方便后续按需启用。
export const request = createRequest({
  baseURL: getApiBase(),
  loading: false,
  errorToast: true,
  cancelRepeat: true,
  unwrap: true,
})
