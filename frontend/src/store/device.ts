import { proxy } from 'valtio'

const SIDEBAR_COLLAPSED_KEY = 'base_layout_sidebar_collapsed'

const state = proxy({
  chatting: false,
  sidebarCollapsed: (() => {
    if (typeof window === 'undefined') return false
    return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === 'true'
  })(),
})

const actions = {
  setChatting(chatting: boolean) {
    state.chatting = chatting
  },
  setSidebarCollapsed(collapsed: boolean) {
    state.sidebarCollapsed = collapsed
    try {
      localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(collapsed))
    } catch {
      // ignore
    }
  },
  toggleSidebar() {
    actions.setSidebarCollapsed(!state.sidebarCollapsed)
  },
}

export const deviceState = state
export const deviceActions = actions
