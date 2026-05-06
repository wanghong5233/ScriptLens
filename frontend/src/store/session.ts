import { proxy } from 'valtio'

const state = proxy({
  list: [] as API.Session[],
  updateKey: 0,
})

const actions = {
  setList(list: API.Session[]) {
    state.list = list
  },
  add(item: API.Session) {
    state.list.push(item)
  },
  remove(sessionId: string) {
    state.list = state.list.filter((item) => item.session_id !== sessionId)
  },
  updateKey() {
    state.updateKey += 1
  },
  clear() {
    state.list = []
    state.updateKey += 1
  },
}

export const sessionState = state
export const sessionActions = actions
