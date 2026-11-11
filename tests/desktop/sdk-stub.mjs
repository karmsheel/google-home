// Stand-in for @hermes/plugin-sdk. Exposes exactly the names googlehome/desktop/plugin.js
// imports, with atoms and hooks that work outside React so components can be rendered.

function makeAtom(initial) {
  return {
    value: initial,
    get() {
      return this.value
    },
    set(next) {
      this.value = next
    },
    subscribe() {
      return () => {}
    }
  }
}

export function atom(initial) {
  return makeAtom(initial)
}

export function useValue(atomRef) {
  return atomRef && typeof atomRef.get === 'function' ? atomRef.get() : atomRef
}

export function haptic() {}

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const COMPOSER_AREAS = { actions: 'composer.actions' }
export const SESSION_ROW_AREAS = { leading: 'sessionRow.leading', trailing: 'sessionRow.trailing' }

export const host = {
  state: {
    profile: makeAtom('coder'),
    gateway: makeAtom('open'),
    focusedSessionId: makeAtom('session-1'),
    focusedStoredSessionId: makeAtom('session-1')
  },
  navigate() {},
  notify() {},
  notifyError() {},
  request: async () => ({}),
  requestProfile: async () => ({ text: '' }),
  openSession: async () => {},
  // The two sessions every test sees: one from the speaker, one ordinary chat.
  listPersistedSessions: async () => ({
    sessions: [
      {
        id: '20261109_200058_ad554bed',
        source: 'googlehome',
        title: 'Greet room and name model',
        last_active: 1794340558,
        started_at: 1794247258,
        profile: 'coder'
      },
      {
        id: 'local-chat-1',
        source: 'desktop',
        title: 'Some other chat',
        last_active: 1794300000,
        started_at: 1794200000,
        profile: 'coder'
      }
    ]
  }),
  profiles: {
    list: async () => ({ profiles: [{ name: 'coder' }, { name: 'designer' }] })
  }
}

function component(name) {
  const Comp = props => ({ type: 'div', props: { ...props, 'data-component': name } })
  Comp.displayName = name
  return Comp
}

export const Button = component('Button')
export const ListRow = component('ListRow')
export const Popover = component('Popover')
export const PopoverContent = component('PopoverContent')
export const PopoverTrigger = component('PopoverTrigger')
export const Select = component('Select')
export const SelectContent = component('SelectContent')
export const SelectItem = component('SelectItem')
export const SelectTrigger = component('SelectTrigger')
export const SelectValue = component('SelectValue')
export const Switch = component('Switch')
export const Tip = component('Tip')
export const icons = {
  Volume2: component('Volume2'),
  VolumeX: component('VolumeX')
}
